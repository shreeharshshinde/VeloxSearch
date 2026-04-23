// cmd/main.go
// ============
// Crawler Service — entrypoint and worker pool orchestrator.
//
// This binary:
//  1. Connects to Redis and MinIO
//  2. Spins up N goroutine workers (default 10, set via CRAWLER_WORKERS)
//  3. Each worker pops URL tasks from the Redis priority queue
//  4. Checks robots.txt, applies rate limiting
//  5. Fetches the page (static pages only; JS pages deferred to Python)
//  6. Stores raw HTML in MinIO (content-addressed, gzip-compressed)
//  7. Publishes a crawl_complete event to Redis Stream for the Processor
//  8. Logs timing metrics to PostgreSQL crawl_log table
//
// ARCHITECTURE:
//   One goroutine per worker. Workers share:
//     - HTTPFetcher (shared http.Client + connection pool)
//     - RobotsCache (shared Redis-backed robots.txt cache)
//     - Limiter     (shared Redis-backed per-domain rate limiter)
//     - StorageClient (shared MinIO connection pool)
//
// GRACEFUL SHUTDOWN:
//   On SIGINT/SIGTERM, the context is cancelled. Workers finish
//   their current fetch, then exit cleanly.

package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/shreeharshshinde/VeloxSearch/services/crawler/internal/fetcher"
	"github.com/shreeharshshinde/VeloxSearch/services/crawler/internal/ratelimit"
	"github.com/shreeharshshinde/VeloxSearch/services/crawler/internal/storage"
)

// URLTask mirrors the Python URLTask dataclass — populated by the Discovery service.
type URLTask struct {
	URL             string                 `json:"url"`
	Source          string                 `json:"source"`
	Domain          string                 `json:"domain"`
	DiscoveredAt    float64                `json:"discovered_at"`
	Depth           int                    `json:"depth"`
	DomainAuthority float64                `json:"domain_authority"`
	Priority        float64                `json:"priority"`
	Metadata        map[string]interface{} `json:"metadata"`
}

// CrawlEvent is published to Redis Stream STREAM:crawl_complete.
// The Processor service (Python) consumes these events.
type CrawlEvent struct {
	URL             string  `json:"url"`
	OriginalURL     string  `json:"original_url"`
	Domain          string  `json:"domain"`
	Status          string  `json:"status"`
	FetcherType     string  `json:"fetcher_type"`
	HTTPStatus      int     `json:"http_status"`
	ContentType     string  `json:"content_type"`
	ContentHash     string  `json:"content_hash"`
	MinIOKey        string  `json:"minio_key"`
	FetchBytes      int     `json:"fetch_bytes"`
	FetchMs         int64   `json:"fetch_ms"`
	CrawledAt       float64 `json:"crawled_at"`
	DiscoveredAt    float64 `json:"discovered_at"`
	QueueWaitMs     int64   `json:"queue_wait_ms"`
	Depth           int     `json:"depth"`
	DiscoverySource string  `json:"discovery_source"`
	Error           string  `json:"error"`
}

func main() {
	// ── Logging ───────────────────────────────────────────────────────────────
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))
	slog.SetDefault(logger)

	// ── Config from environment ───────────────────────────────────────────────
	cfg := loadConfig()

	// ── Context with graceful shutdown ────────────────────────────────────────
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigCh
		slog.Info("shutdown signal received", "signal", sig)
		cancel()
	}()

	// ── Redis connection ──────────────────────────────────────────────────────
	redisClient := redis.NewClient(&redis.Options{
		Addr:     cfg.RedisAddr,
		Password: cfg.RedisPassword,
		DB:       0,
		PoolSize: cfg.Workers * 3, // 3 connections per worker
	})
	if err := redisClient.Ping(ctx).Err(); err != nil {
		slog.Error("redis connection failed", "error", err)
		os.Exit(1)
	}
	slog.Info("redis connected", "addr", cfg.RedisAddr)

	// ── Shared components ─────────────────────────────────────────────────────
	robotsCache := fetcher.NewRobotsCache(redisClient)
	limiter := ratelimit.NewLimiter(redisClient)

	storageClient, err := storage.NewStorageClient(
		redisClient,
		cfg.MinIOEndpoint, cfg.MinIOAccessKey, cfg.MinIOSecretKey, cfg.MinIOBucket,
		cfg.MinIOSecure,
	)
	if err != nil {
		slog.Error("storage client init failed", "error", err)
		os.Exit(1)
	}

	if err := storageClient.EnsureBucket(ctx); err != nil {
		slog.Error("bucket setup failed", "error", err)
		os.Exit(1)
	}
	slog.Info("minio connected", "endpoint", cfg.MinIOEndpoint, "bucket", cfg.MinIOBucket)

	httpFetcher := fetcher.NewHTTPFetcher(robotsCache, limiter, storageClient, fetcher.Config{
		UserAgent:    cfg.UserAgent,
		Timeout:      time.Duration(cfg.TimeoutSec) * time.Second,
		MaxRedirects: cfg.MaxRedirects,
	})

	// ── Worker pool ───────────────────────────────────────────────────────────
	slog.Info("starting crawler worker pool", "workers", cfg.Workers)

	var wg sync.WaitGroup
	for i := 0; i < cfg.Workers; i++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			runWorker(ctx, workerID, redisClient, httpFetcher, storageClient, cfg)
		}(i)
	}

	slog.Info("crawler service running", "workers", cfg.Workers, "queue", cfg.QueueKey)

	// Wait for all workers to finish (after context cancellation)
	wg.Wait()
	slog.Info("crawler service stopped gracefully")
}

// runWorker is the main loop for a single crawler goroutine.
// It continuously pops URL tasks from the queue and fetches them.
func runWorker(
	ctx context.Context,
	id int,
	redisClient *redis.Client,
	httpFetcher *fetcher.HTTPFetcher,
	store *storage.StorageClient,
	cfg *Config,
) {
	slog.Info("worker started", "worker_id", id)

	for {
		// Pop next task — blocks up to 5 seconds if queue is empty
		rawItems, err := redisClient.BZPopMax(ctx, 5*time.Second, cfg.QueueKey).Result()
		if err != nil {
			if ctx.Err() != nil {
				return // context cancelled — normal shutdown
			}
			if err == redis.Nil {
				continue // queue empty — keep waiting
			}
			slog.Error("queue pop error", "worker_id", id, "error", err)
			time.Sleep(time.Second)
			continue
		}

		// Deserialise URLTask
		var task URLTask
		if err := json.Unmarshal([]byte(rawItems.Member.(string)), &task); err != nil {
			slog.Error("task deserialise error", "error", err)
			continue
		}

		// Process the URL
		processURL(ctx, id, task, redisClient, httpFetcher, store, cfg)
	}
}

// processURL runs the full fetch → store → publish pipeline for one URL.
func processURL(
	ctx context.Context,
	workerID int,
	task URLTask,
	redisClient *redis.Client,
	httpFetcher *fetcher.HTTPFetcher,
	store *storage.StorageClient,
	cfg *Config,
) {
	queueWaitMs := int64((timeNow() - task.DiscoveredAt) * 1000)
	slog.Info("crawling",
		"worker_id", workerID,
		"url", task.URL,
		"source", task.Source,
		"queue_wait_ms", queueWaitMs,
	)

	event := CrawlEvent{
		URL:             task.URL,
		OriginalURL:     task.URL,
		Domain:          task.Domain,
		FetcherType:     "http",
		DiscoveredAt:    task.DiscoveredAt,
		QueueWaitMs:     queueWaitMs,
		Depth:           task.Depth,
		DiscoverySource: task.Source,
	}

	// ── Fetch the page ────────────────────────────────────────────────────────
	result := httpFetcher.Fetch(ctx, task.URL)

	event.URL = result.URL
	event.HTTPStatus = result.HTTPStatus
	event.ContentType = result.ContentType
	event.FetchMs = result.FetchMs
	event.CrawledAt = timeNow()

	if result.Error != nil {
		errMsg := result.Error.Error()
		event.Error = errMsg

		if result.HTTPStatus == -1 {
			event.Status = "robots_block"
		} else if result.HTTPStatus >= 400 {
			event.Status = "http_error"
		} else {
			event.Status = "fetch_error"
		}

		slog.Warn("crawl failed",
			"url", task.URL,
			"status", event.Status,
			"error", errMsg,
		)
		publishCrawlEvent(ctx, redisClient, cfg.CrawlCompleteStream, event)
		return
	}

	// ── Store in MinIO ────────────────────────────────────────────────────────
	storeResult, err := store.Store(ctx, task.Domain, result.Body)
	if err != nil {
		slog.Error("storage failed", "url", task.URL, "error", err)
		event.Status = "storage_error"
		event.Error = err.Error()
		publishCrawlEvent(ctx, redisClient, cfg.CrawlCompleteStream, event)
		return
	}

	event.ContentHash = storeResult.ContentHash
	event.MinIOKey = storeResult.MinIOKey
	event.FetchBytes = len(result.Body)

	if storeResult.WasDuplicate {
		event.Status = "duplicate"
		slog.Info("duplicate content — skipping processing",
			"url", task.URL,
			"hash", storeResult.ContentHash,
		)
	} else {
		event.Status = "success"
		slog.Info("crawled successfully",
			"url", task.URL,
			"bytes", len(result.Body),
			"compressed_bytes", storeResult.Bytes,
			"fetch_ms", result.FetchMs,
			"minio_key", storeResult.MinIOKey,
		)
	}

	// ── Publish crawl complete event ──────────────────────────────────────────
	publishCrawlEvent(ctx, redisClient, cfg.CrawlCompleteStream, event)
}

// publishCrawlEvent writes a CrawlEvent to the Redis Stream.
// The Processor service consumes this stream to begin NLP processing.
func publishCrawlEvent(ctx context.Context, r *redis.Client, streamKey string, event CrawlEvent) {
	data, err := json.Marshal(event)
	if err != nil {
		slog.Error("event marshal error", "error", err)
		return
	}

	// Redis Stream XADD — auto-generates message ID
	if err := r.XAdd(ctx, &redis.XAddArgs{
		Stream: streamKey,
		Values: map[string]interface{}{
			"data": string(data),
		},
		MaxLen: 100_000, // cap stream at 100k events
		Approx: true,
	}).Err(); err != nil {
		slog.Error("stream publish error", "error", err)
	}
}

func timeNow() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}

// ── Config ────────────────────────────────────────────────────────────────────

type Config struct {
	Workers             int
	QueueKey            string
	CrawlCompleteStream string

	RedisAddr     string
	RedisPassword string

	MinIOEndpoint  string
	MinIOAccessKey string
	MinIOSecretKey string
	MinIOBucket    string
	MinIOSecure    bool

	UserAgent    string
	TimeoutSec   int
	MaxRedirects int
}

func loadConfig() *Config {
	return &Config{
		Workers:             envInt("CRAWLER_WORKERS", 10),
		QueueKey:            envStr("QUEUE_KEY", "QUEUE:urls"),
		CrawlCompleteStream: envStr("CRAWL_COMPLETE_STREAM", "STREAM:crawl_complete"),

		RedisAddr:     envStr("REDIS_ADDR", "localhost:6379"),
		RedisPassword: envStr("REDIS_PASSWORD", ""),

		MinIOEndpoint:  envStr("MINIO_ENDPOINT", "localhost:9000"),
		MinIOAccessKey: envStr("MINIO_ACCESS_KEY", "minioadmin"),
		MinIOSecretKey: envStr("MINIO_SECRET_KEY", "minioadmin"),
		MinIOBucket:    envStr("MINIO_BUCKET", "raw-pages"),
		MinIOSecure:    envBool("MINIO_SECURE", false),

		UserAgent:    envStr("CRAWLER_USER_AGENT", "RealtimeIndexer/1.0"),
		TimeoutSec:   envInt("CRAWLER_TIMEOUT_SECONDS", 30),
		MaxRedirects: envInt("MAX_REDIRECTS", 5),
	}
}

func envStr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	if v := os.Getenv(key); v != "" {
		return v == "true" || v == "1"
	}
	return fallback
}
