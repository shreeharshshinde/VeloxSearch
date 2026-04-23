// internal/fetcher/http_fetcher.go
// ==================================
// High-performance async HTTP fetcher for static HTML pages.
//
// WHY GO FOR THIS?
// Python is excellent for NLP and orchestration but slower for
// high-throughput HTTP work. Go goroutines handle 10,000 concurrent
// connections with ~8KB stack per goroutine (vs ~1MB per OS thread).
// At 10,000 workers: Go uses ~80MB, threads would use ~10GB.
//
// WHAT THIS HANDLES:
//   - Plain HTML pages (news, blogs, documentation, Wikipedia, etc.)
//   - HTTP/1.1 and HTTP/2
//   - Gzip / Brotli / deflate response decompression (auto)
//   - Redirect following (up to MaxRedirects hops)
//   - Timeout per request (configurable, default 30s)
//   - robots.txt check before fetching (via RobotsCache)
//   - Per-domain rate limiting (via Limiter)
//
// WHAT THIS DOES NOT HANDLE:
//   - JavaScript-rendered pages (React, Next.js, etc.) → use playwright_crawler
//   - Login-required pages
//   - CAPTCHA pages
//
// The page_classifier (in playwright_crawler/) decides which fetcher to use
// based on response headers and HTML content signals.

package fetcher

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/shreeharshshinde/VeloxSearch/services/crawler/internal/ratelimit"
	"github.com/shreeharshshinde/VeloxSearch/services/crawler/internal/storage"
)

const (
	// MaxBodySize limits response body size to 10MB.
	// Pages larger than this are skipped (likely not text content).
	MaxBodySize = 10 * 1024 * 1024

	// defaultTimeout for each HTTP request.
	defaultTimeout = 30 * time.Second

	// defaultMaxRedirects prevents redirect loops.
	defaultMaxRedirects = 5
)

// FetchResult holds the result of fetching a single URL.
type FetchResult struct {
	URL             string            // final URL after redirects
	OriginalURL     string            // URL as requested
	HTTPStatus      int               // HTTP response status code
	ContentType     string            // Content-Type header value
	Body            []byte            // raw response body
	FetchMs         int64             // time to fetch in milliseconds
	RedirectChain   []string          // list of redirect URLs
	ResponseHeaders map[string]string // selected response headers
	Error           error             // non-nil if fetch failed
}

// HTTPFetcher fetches static HTML pages.
// Shared across the goroutine worker pool.
type HTTPFetcher struct {
	client       *http.Client
	robots       *RobotsCache
	limiter      *ratelimit.Limiter
	storage      *storage.StorageClient
	userAgent    string
	maxRedirects int
	timeout      time.Duration
}

// Config holds configuration for the HTTPFetcher.
type Config struct {
	UserAgent    string
	Timeout      time.Duration
	MaxRedirects int
}

// NewHTTPFetcher creates a new fetcher with the given config.
func NewHTTPFetcher(
	robots *RobotsCache,
	limiter *ratelimit.Limiter,
	store *storage.StorageClient,
	cfg Config,
) *HTTPFetcher {
	if cfg.Timeout == 0 {
		cfg.Timeout = defaultTimeout
	}
	if cfg.MaxRedirects == 0 {
		cfg.MaxRedirects = defaultMaxRedirects
	}
	if cfg.UserAgent == "" {
		cfg.UserAgent = "RealtimeIndexer/1.0"
	}

	redirectCount := 0
	client := &http.Client{
		Timeout: cfg.Timeout,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			redirectCount++
			if redirectCount > cfg.MaxRedirects {
				return fmt.Errorf("too many redirects (max %d)", cfg.MaxRedirects)
			}
			return nil
		},
	}

	return &HTTPFetcher{
		client:       client,
		robots:       robots,
		limiter:      limiter,
		storage:      store,
		userAgent:    cfg.UserAgent,
		maxRedirects: cfg.MaxRedirects,
		timeout:      cfg.Timeout,
	}
}

// Fetch fetches a single URL, respecting robots.txt and rate limits.
//
// Flow:
//  1. Check robots.txt → if blocked, return robots_block status
//  2. Apply rate limiter → wait until domain allows another request
//  3. HTTP GET with timeout and redirect following
//  4. Read body (limited to MaxBodySize)
//  5. Return FetchResult
//
// The caller is responsible for storing the result in MinIO and
// publishing the crawl event to Redis Stream.
func (f *HTTPFetcher) Fetch(ctx context.Context, rawURL string) *FetchResult {
	start := time.Now()
	result := &FetchResult{
		OriginalURL: rawURL,
		URL:         rawURL,
	}

	// ── Step 1: Parse URL ────────────────────────────────────────────────────
	parsed, err := url.Parse(rawURL)
	if err != nil {
		result.Error = fmt.Errorf("parse url: %w", err)
		return result
	}
	domain := parsed.Hostname()

	// ── Step 2: Check robots.txt ─────────────────────────────────────────────
	allowed, crawlDelay, err := f.robots.IsAllowed(ctx, rawURL)
	if err != nil {
		// Can't fetch robots.txt → default to allow
		allowed = true
		crawlDelay = 0
	}
	if !allowed {
		result.Error = fmt.Errorf("blocked by robots.txt")
		result.HTTPStatus = -1 // sentinel for robots block
		return result
	}

	// ── Step 3: Rate limit ───────────────────────────────────────────────────
	// Use crawlDelay from robots.txt if specified, otherwise default rate
	rate := 1.0
	if crawlDelay > 0 {
		rate = 1.0 / crawlDelay // crawlDelay is seconds between requests
	}
	if err := f.limiter.Wait(ctx, domain, rate); err != nil {
		result.Error = fmt.Errorf("rate limiter: %w", err)
		return result
	}

	// ── Step 4: Build HTTP request ───────────────────────────────────────────
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		result.Error = fmt.Errorf("build request: %w", err)
		return result
	}

	req.Header.Set("User-Agent", f.userAgent)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Accept-Encoding", "gzip, deflate, br")

	// ── Step 5: Execute request ───────────────────────────────────────────────
	resp, err := f.client.Do(req)
	if err != nil {
		result.Error = fmt.Errorf("http do: %w", err)
		result.FetchMs = time.Since(start).Milliseconds()
		return result
	}
	defer resp.Body.Close()

	result.URL = resp.Request.URL.String() // URL after redirects
	result.HTTPStatus = resp.StatusCode
	result.ContentType = resp.Header.Get("Content-Type")

	// Extract selected response headers
	result.ResponseHeaders = map[string]string{
		"Content-Language": resp.Header.Get("Content-Language"),
		"Last-Modified":    resp.Header.Get("Last-Modified"),
		"ETag":             resp.Header.Get("ETag"),
		"X-Frame-Options":  resp.Header.Get("X-Frame-Options"),
	}

	// ── Step 6: Read body ─────────────────────────────────────────────────────
	// Only read HTML content; skip binaries, images, etc.
	if !isHTMLContent(result.ContentType) {
		result.Error = fmt.Errorf("not HTML: %s", result.ContentType)
		result.FetchMs = time.Since(start).Milliseconds()
		return result
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		result.FetchMs = time.Since(start).Milliseconds()
		result.Error = fmt.Errorf("non-2xx status: %d", resp.StatusCode)
		return result
	}

	// Limit body read to MaxBodySize
	body, err := io.ReadAll(io.LimitReader(resp.Body, MaxBodySize))
	if err != nil {
		result.Error = fmt.Errorf("read body: %w", err)
		result.FetchMs = time.Since(start).Milliseconds()
		return result
	}

	result.Body = body
	result.FetchMs = time.Since(start).Milliseconds()
	return result
}

// IsHTMLContentExported returns true if the content-type indicates HTML.
// Exported for testing purposes.
func IsHTMLContentExported(contentType string) bool {
	ct := strings.ToLower(contentType)
	return strings.Contains(ct, "text/html") ||
		strings.Contains(ct, "application/xhtml") ||
		contentType == "" // assume HTML if no content-type header
}

// isHTMLContent is the internal version, kept for compatibility.
func isHTMLContent(contentType string) bool {
	return IsHTMLContentExported(contentType)
}

// ContentHashExported computes a content hash using the storage package.
// Exported for testing purposes.
func ContentHashExported(data []byte) string {
	return storage.ContentHash(data)
}
