// tests/fetcher_test.go
// ======================
// Unit tests for the Go crawler components.
// Run with: go test ./... -v

package tests

import (
	"testing"

	"github.com/shreeharshshinde/VeloxSearch/services/crawler/internal/fetcher"
)

// ── robots.go tests ────────────────────────────────────────────────────────────

func TestParseRobots_AllowAll(t *testing.T) {
	content := `
User-agent: *
Allow: /
`
	allowed, delay := parseRobots(content, "/any/path", "RealtimeIndexer")
	if !allowed {
		t.Error("expected allowed=true for Allow: /")
	}
	if delay != 0 {
		t.Errorf("expected delay=0, got %f", delay)
	}
}

func TestParseRobots_DisallowAll(t *testing.T) {
	content := `
User-agent: *
Disallow: /
`
	allowed, _ := parseRobots(content, "/", "RealtimeIndexer")
	if allowed {
		t.Error("expected allowed=false for Disallow: /")
	}
}

func TestParseRobots_DisallowSpecificPath(t *testing.T) {
	content := `
User-agent: *
Disallow: /private/
Disallow: /admin/
`
	// Private path should be blocked
	allowed, _ := parseRobots(content, "/private/data", "RealtimeIndexer")
	if allowed {
		t.Error("expected /private/data to be disallowed")
	}

	// Admin path should be blocked
	allowed, _ = parseRobots(content, "/admin/login", "RealtimeIndexer")
	if allowed {
		t.Error("expected /admin/login to be disallowed")
	}

	// Other paths should be allowed
	allowed, _ = parseRobots(content, "/public/page", "RealtimeIndexer")
	if !allowed {
		t.Error("expected /public/page to be allowed")
	}
}

func TestParseRobots_AllowOverridesDisallow(t *testing.T) {
	content := `
User-agent: *
Disallow: /products/
Allow: /products/public/
`
	// /products/private/ is disallowed
	allowed, _ := parseRobots(content, "/products/private/item", "RealtimeIndexer")
	if allowed {
		t.Error("expected /products/private to be disallowed")
	}

	// /products/public/ is explicitly allowed (longer Allow wins)
	allowed, _ = parseRobots(content, "/products/public/item", "RealtimeIndexer")
	if !allowed {
		t.Error("expected /products/public to be allowed (Allow overrides Disallow)")
	}
}

func TestParseRobots_CrawlDelay(t *testing.T) {
	content := `
User-agent: *
Disallow:
Crawl-delay: 5
`
	allowed, delay := parseRobots(content, "/page", "RealtimeIndexer")
	if !allowed {
		t.Error("expected allowed=true")
	}
	if delay != 5.0 {
		t.Errorf("expected delay=5.0, got %f", delay)
	}
}

func TestParseRobots_AgentSpecificRules(t *testing.T) {
	content := `
User-agent: *
Disallow:

User-agent: RealtimeIndexer
Disallow: /secret/
Crawl-delay: 2
`
	// Our agent is blocked from /secret/
	allowed, delay := parseRobots(content, "/secret/data", "RealtimeIndexer")
	if allowed {
		t.Error("expected RealtimeIndexer to be blocked from /secret/")
	}
	if delay != 2.0 {
		t.Errorf("expected delay=2.0 for our agent, got %f", delay)
	}

	// Our agent allowed elsewhere
	allowed, _ = parseRobots(content, "/public/page", "RealtimeIndexer")
	if !allowed {
		t.Error("expected RealtimeIndexer to be allowed on /public/page")
	}
}

func TestParseRobots_EmptyContent(t *testing.T) {
	// Empty robots.txt = allow everything
	allowed, delay := parseRobots("", "/any/path", "RealtimeIndexer")
	if !allowed {
		t.Error("expected allowed=true for empty robots.txt")
	}
	if delay != 0 {
		t.Errorf("expected delay=0 for empty robots.txt, got %f", delay)
	}
}

func TestParseRobots_CommentsIgnored(t *testing.T) {
	content := `
# This is a comment
User-agent: * # inline comment
Disallow: /blocked/ # block this
Allow: /allowed/   # allow this
`
	blocked, _ := parseRobots(content, "/blocked/page", "RealtimeIndexer")
	if blocked {
		t.Error("expected /blocked/ to be disallowed")
	}

	allowed, _ := parseRobots(content, "/allowed/page", "RealtimeIndexer")
	if !allowed {
		t.Error("expected /allowed/ to be allowed")
	}
}

// ── HTTP fetcher tests ─────────────────────────────────────────────────────────

func TestIsHTMLContent(t *testing.T) {
	cases := []struct {
		contentType string
		expected    bool
	}{
		{"text/html; charset=utf-8", true},
		{"text/html", true},
		{"application/xhtml+xml", true},
		{"", true}, // no content-type = assume HTML
		{"application/json", false},
		{"image/png", false},
		{"application/pdf", false},
		{"text/css", false},
	}

	for _, tc := range cases {
		result := fetcher.IsHTMLContentExported(tc.contentType)
		if result != tc.expected {
			t.Errorf("isHTMLContent(%q) = %v, want %v", tc.contentType, result, tc.expected)
		}
	}
}

// ── Storage tests ──────────────────────────────────────────────────────────────

func TestContentHash_Consistent(t *testing.T) {
	// Same content → same hash every time
	data := []byte("<html><body>Hello World</body></html>")
	h1 := fetcher.ContentHashExported(data)
	h2 := fetcher.ContentHashExported(data)

	if h1 != h2 {
		t.Errorf("inconsistent hash: %q != %q", h1, h2)
	}
	if len(h1) != 16 {
		t.Errorf("expected 16-char hash, got %d chars: %q", len(h1), h1)
	}
}

func TestContentHash_DifferentContent(t *testing.T) {
	h1 := fetcher.ContentHashExported([]byte("page version 1"))
	h2 := fetcher.ContentHashExported([]byte("page version 2"))

	if h1 == h2 {
		t.Error("different content should produce different hashes")
	}
}

// ── Helper: expose internal functions for testing ──────────────────────────────
// (In production code, these are unexported. We test them through exported wrappers.)

func parseRobots(content, path, agent string) (bool, float64) {
	return fetcher.ParseRobotsExported(content, path, agent)
}
