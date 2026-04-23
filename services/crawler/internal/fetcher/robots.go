// internal/fetcher/robots.go
// ===========================
// robots.txt parser and per-domain cache.
//
// WHY THIS EXISTS:
// Before fetching any URL, we must check if the site allows crawling that path.
// Fetching robots.txt on every request would:
//   a) slow down crawling (extra HTTP round-trip per URL)
//   b) hammer the site's server
//
// Solution: cache robots.txt per domain in Redis with a 24-hour TTL.
// The RobotsCache is shared across the entire crawler worker pool.
//
// ROBOTS.TXT RULES WE HONOUR:
//   User-agent: *        (or our specific bot name)
//   Disallow: /path      (block this path)
//   Allow: /path         (explicitly allow, overrides Disallow)
//   Crawl-delay: N       (wait N seconds between requests to this domain)
//
// REFERENCE: https://www.rfc-editor.org/rfc/rfc9309 (robots.txt RFC)

package fetcher

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	// robotsCacheTTL is how long we cache a domain's robots.txt.
	// 24 hours is a sensible balance between freshness and performance.
	robotsCacheTTL = 24 * time.Hour

	// robotsKeyPrefix is the Redis key prefix for cached robots.txt content.
	// Full key: ROBOTS:{domain}  e.g. ROBOTS:example.com
	robotsKeyPrefix = "ROBOTS:"

	// crawlDelayKeyPrefix stores the Crawl-delay value per domain.
	// Full key: CRAWLDELAY:{domain}
	crawlDelayKeyPrefix = "CRAWLDELAY:"

	// userAgent is what we send in the User-Agent header.
	// Sites can use this to apply specific rules to our bot.
	userAgent = "RealtimeIndexer"
)

// RobotsCache fetches, parses, and caches robots.txt per domain.
// Thread-safe — safe to use from multiple goroutines.
type RobotsCache struct {
	redis      *redis.Client
	httpClient *http.Client
}

// NewRobotsCache creates a new RobotsCache backed by the given Redis client.
func NewRobotsCache(redisClient *redis.Client) *RobotsCache {
	return &RobotsCache{
		redis: redisClient,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
			// Do not follow redirects for robots.txt — if it redirects,
			// we treat the domain as having no robots.txt restrictions.
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

// IsAllowed returns true if the given URL is allowed to be crawled.
// It fetches robots.txt from the domain on first call, then caches it.
//
// Returns (true, 0) if allowed with no crawl delay.
// Returns (false, 0) if blocked by robots.txt.
// Returns (true, N) if allowed but must wait N seconds between requests.
func (r *RobotsCache) IsAllowed(ctx context.Context, rawURL string) (allowed bool, crawlDelay float64, err error) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return false, 0, fmt.Errorf("parse url: %w", err)
	}

	domain := parsed.Hostname()
	path := parsed.Path
	if path == "" {
		path = "/"
	}

	// Get robots.txt content from cache or fetch it
	content, err := r.getRobotsContent(ctx, domain, parsed.Scheme)
	if err != nil {
		// If we can't fetch robots.txt, default to allowing crawl
		// (better to crawl something we shouldn't than to miss content)
		return true, 0, nil
	}

	// Parse the robots.txt content and check if path is allowed
	allowed, crawlDelay = parseRobots(content, path, userAgent)
	return allowed, crawlDelay, nil
}

// CrawlDelay returns the Crawl-delay for a domain (cached).
// Returns 0 if no delay is specified.
func (r *RobotsCache) CrawlDelay(ctx context.Context, domain string) float64 {
	key := crawlDelayKeyPrefix + domain
	val, err := r.redis.Get(ctx, key).Float64()
	if err != nil {
		return 0
	}
	return val
}

// getRobotsContent returns the cached robots.txt for a domain.
// If not cached, fetches it from the site and caches it.
func (r *RobotsCache) getRobotsContent(ctx context.Context, domain, scheme string) (string, error) {
	cacheKey := robotsKeyPrefix + domain

	// Try cache first
	cached, err := r.redis.Get(ctx, cacheKey).Result()
	if err == nil {
		return cached, nil
	}
	if err != redis.Nil {
		return "", fmt.Errorf("redis get: %w", err)
	}

	// Cache miss — fetch from site
	robotsURL := fmt.Sprintf("%s://%s/robots.txt", scheme, domain)
	content, err := r.fetchRobotsTxt(ctx, robotsURL)
	if err != nil {
		// Store empty string so we don't retry on every request
		_ = r.redis.Set(ctx, cacheKey, "", robotsCacheTTL).Err()
		return "", nil // no robots.txt = allow everything
	}

	// Cache the content
	if err := r.redis.Set(ctx, cacheKey, content, robotsCacheTTL).Err(); err != nil {
		// Non-fatal — we still have the content for this request
		_ = err
	}

	// Also cache the crawl delay separately for fast lookup
	_, delay := parseRobots(content, "/", userAgent)
	if delay > 0 {
		_ = r.redis.Set(ctx, crawlDelayKeyPrefix+domain, delay, robotsCacheTTL).Err()
	}

	return content, nil
}

// fetchRobotsTxt performs the actual HTTP fetch of robots.txt.
func (r *RobotsCache) fetchRobotsTxt(ctx context.Context, robotsURL string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, robotsURL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", userAgent+"/1.0")

	resp, err := r.httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	// 4xx/5xx with no robots.txt = allow everything
	if resp.StatusCode == 404 || resp.StatusCode >= 500 {
		return "", nil
	}
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("unexpected status %d", resp.StatusCode)
	}

	// Limit to 500KB to avoid memory issues with malformed robots.txt
	body, err := io.ReadAll(io.LimitReader(resp.Body, 512*1024))
	if err != nil {
		return "", err
	}
	return string(body), nil
}

// parseRobots parses robots.txt content and returns whether the given path
// is allowed for the given user agent.
//
// Implements the subset of robots.txt rules that matter for our crawler:
//   - User-agent: * and User-agent: RealtimeIndexer
//   - Disallow: /path
//   - Allow: /path
//   - Crawl-delay: N
//
// Rules for a specific user-agent take precedence over wildcard rules.
func ParseRobotsExported(content, path, agent string) (allowed bool, crawlDelay float64) {
	return parseRobots(content, path, agent)
}

// parseRobots is the internal version that implements robots.txt parsing.
func parseRobots(content, path, agent string) (allowed bool, crawlDelay float64) {
	lines := strings.Split(strings.ReplaceAll(content, "\r\n", "\n"), "\n")

	// We track two rule sets: one for wildcard (*) and one for our agent
	type ruleSet struct {
		disallow   []string
		allow      []string
		crawlDelay float64
		active     bool // are we in a block matching our agent?
	}

	var wildcard, specific ruleSet
	var currentIsSpecific, currentIsWildcard bool

	for _, line := range lines {
		// Strip comments
		if idx := strings.Index(line, "#"); idx >= 0 {
			line = line[:idx]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			// Empty line = end of a rule block
			currentIsSpecific = false
			currentIsWildcard = false
			continue
		}

		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		directive := strings.TrimSpace(strings.ToLower(parts[0]))
		value := strings.TrimSpace(parts[1])

		switch directive {
		case "user-agent":
			agentLower := strings.ToLower(value)
			if agentLower == strings.ToLower(agent) {
				currentIsSpecific = true
				currentIsWildcard = false
				specific.active = true
			} else if agentLower == "*" {
				currentIsWildcard = true
				currentIsSpecific = false
				wildcard.active = true
			} else {
				currentIsSpecific = false
				currentIsWildcard = false
			}
		case "disallow":
			if currentIsSpecific {
				specific.disallow = append(specific.disallow, value)
			} else if currentIsWildcard {
				wildcard.disallow = append(wildcard.disallow, value)
			}
		case "allow":
			if currentIsSpecific {
				specific.allow = append(specific.allow, value)
			} else if currentIsWildcard {
				wildcard.allow = append(wildcard.allow, value)
			}
		case "crawl-delay":
			var delay float64
			fmt.Sscanf(value, "%f", &delay)
			if currentIsSpecific {
				specific.crawlDelay = delay
			} else if currentIsWildcard {
				wildcard.crawlDelay = delay
			}
		}
	}

	// Apply specific-agent rules first, fall back to wildcard
	active := wildcard
	if specific.active {
		active = specific
	}

	crawlDelay = active.crawlDelay

	// Check if path is allowed
	// Algorithm: Allow overrides Disallow for the most specific matching prefix
	longestDisallow := -1
	longestAllow := -1

	for _, d := range active.disallow {
		if d == "" {
			continue
		}
		if strings.HasPrefix(path, d) && len(d) > longestDisallow {
			longestDisallow = len(d)
		}
	}
	for _, a := range active.allow {
		if a == "" {
			continue
		}
		if strings.HasPrefix(path, a) && len(a) > longestAllow {
			longestAllow = len(a)
		}
	}

	// If no disallow rule matches → allowed
	if longestDisallow < 0 {
		return true, crawlDelay
	}
	// If allow rule is longer or equal → allowed (Allow takes precedence)
	if longestAllow >= longestDisallow {
		return true, crawlDelay
	}
	return false, crawlDelay
}
