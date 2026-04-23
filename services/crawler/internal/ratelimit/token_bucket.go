// internal/ratelimit/token_bucket.go
// ====================================
// Per-domain token bucket rate limiter backed by Redis.
//
// WHY WE RATE LIMIT:
// Without rate limiting, 10,000 concurrent crawlers could send thousands of
// requests per second to a single site — causing it to block our IP or crash.
// We must be a polite crawler: respect robots.txt Crawl-delay and never
// overwhelm any single domain.
//
// WHY REDIS-BACKED (not in-memory):
// The crawler runs as multiple Docker replicas (3+ instances).
// Each instance shares the same rate limit counter via Redis.
// An in-memory bucket would allow each replica to send at the full rate,
// multiplying the actual load by the number of replicas.
//
// HOW TOKEN BUCKET WORKS:
// - Each domain has a "bucket" with capacity = crawl_rate_per_second tokens.
// - Tokens refill at crawl_rate_per_second per second.
// - Each request consumes 1 token.
// - If the bucket is empty, the caller must wait until a token is available.
//
// IMPLEMENTATION:
// We use a Redis Lua script to atomically check-and-decrement the counter.
// The key expires after 1 second, acting as our time window.
// This is a "leaky bucket" approximation, good enough for our use case.
//
//   Key:    RATELIMIT:{domain}:{unix_second}
//   Value:  number of requests made in this second
//   TTL:    2 seconds (so the key auto-cleans)

package ratelimit

import (
	"context"
	"fmt"
	"math"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	// defaultRate is the default crawl rate if not specified in robots.txt.
	// 1 request per second per domain is polite for most sites.
	defaultRate = 1.0

	// keyPrefix for Redis rate limit counters.
	keyPrefix = "RATELIMIT:"
)

// Limiter enforces per-domain crawl rate limits using Redis.
// Thread-safe — shared across all goroutines in the worker pool.
type Limiter struct {
	redis *redis.Client
}

// NewLimiter creates a rate limiter backed by the given Redis client.
func NewLimiter(redisClient *redis.Client) *Limiter {
	return &Limiter{redis: redisClient}
}

// Wait blocks until the domain's rate limit allows another request.
// It uses the configured rate (from robots.txt Crawl-delay if available).
//
// Args:
//
//	ctx:   Context for cancellation (crawler shutdown, timeout)
//	domain: The domain to rate-limit (e.g. "example.com")
//	rate:   Requests per second allowed. Pass 0 to use defaultRate.
//	         Pass the Crawl-delay from robots.txt (e.g. 2.0 = 1 req/2s).
//
// The function blocks in a sleep-and-retry loop, checking every 100ms.
// Maximum wait: 30 seconds (after which it returns anyway to prevent deadlock).
func (l *Limiter) Wait(ctx context.Context, domain string, rate float64) error {
	if rate <= 0 {
		rate = defaultRate
	}

	// Convert rate (req/sec) to minimum gap between requests (ms)
	gapMs := int64(math.Ceil(1000.0 / rate))

	deadline := time.Now().Add(30 * time.Second)

	for {
		// Check context cancellation
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		if time.Now().After(deadline) {
			// Safety valve — never block more than 30s
			return nil
		}

		allowed, waitMs, err := l.tryAcquire(ctx, domain, gapMs)
		if err != nil {
			// Redis error → allow request (fail open)
			return nil
		}
		if allowed {
			return nil
		}

		// Sleep for the required wait time (or until context cancelled)
		sleep := time.Duration(waitMs) * time.Millisecond
		if sleep > time.Second {
			sleep = time.Second // cap sleep at 1s for responsiveness
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(sleep):
		}
	}
}

// tryAcquire atomically checks if a request is allowed and records it.
// Returns (true, 0, nil) if allowed.
// Returns (false, waitMs, nil) if not allowed — caller should wait waitMs.
func (l *Limiter) tryAcquire(ctx context.Context, domain string, gapMs int64) (bool, int64, error) {
	now := time.Now().UnixMilli()
	// Key = RATELIMIT:{domain}:{second_bucket}
	// We use 1-second buckets; the value is the last request timestamp (ms).
	key := fmt.Sprintf("%s%s", keyPrefix, domain)

	// Lua script: atomically read last request time, update if allowed
	script := redis.NewScript(`
		local key = KEYS[1]
		local now = tonumber(ARGV[1])
		local gap = tonumber(ARGV[2])

		local last = redis.call('GET', key)
		if last == false then
			-- No previous request — allow and record
			redis.call('SET', key, now, 'PX', gap * 2)
			return {1, 0}
		end

		local last_ms = tonumber(last)
		local elapsed = now - last_ms

		if elapsed >= gap then
			-- Enough time has passed — allow and update
			redis.call('SET', key, now, 'PX', gap * 2)
			return {1, 0}
		else
			-- Not enough time — return wait time
			local wait = gap - elapsed
			return {0, wait}
		end
	`)

	result, err := script.Run(ctx, l.redis, []string{key}, now, gapMs).Int64Slice()
	if err != nil {
		return false, 0, fmt.Errorf("rate limit script: %w", err)
	}

	allowed := result[0] == 1
	waitMs := result[1]
	return allowed, waitMs, nil
}

// IsAllowed returns whether a request is currently allowed without blocking.
// Used for monitoring/debugging — does not consume a token.
func (l *Limiter) IsAllowed(ctx context.Context, domain string, rate float64) bool {
	if rate <= 0 {
		rate = defaultRate
	}
	gapMs := int64(math.Ceil(1000.0 / rate))
	key := fmt.Sprintf("%s%s", keyPrefix, domain)

	last, err := l.redis.Get(ctx, key).Int64()
	if err != nil {
		return true // no record = definitely allowed
	}
	elapsed := time.Now().UnixMilli() - last
	return elapsed >= gapMs
}
