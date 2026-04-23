// internal/storage/minio_client.go
// ==================================
// MinIO client — stores raw HTML in S3-compatible object storage.
//
// WHY OBJECT STORAGE FOR RAW HTML?
// The crawler and processor are separate services. We need a durable
// handoff point between them. MinIO (S3-compatible) gives us:
//
//   1. Decoupling: Crawler writes, Processor reads. If Processor crashes,
//      the HTML is safe and replayable — no data loss.
//
//   2. Deduplication: We store by content hash. If a page hasn't changed
//      since the last crawl, we skip re-processing — saving CPU and time.
//
//   3. Debugging: Raw HTML is always available for inspection if something
//      goes wrong in the NLP pipeline.
//
//   4. Reprocessing: If we improve the NLP pipeline, we can reprocess
//      all stored HTML without re-crawling the web.
//
// STORAGE LAYOUT:
//   Bucket: raw-pages
//   Key:    {domain}/{content_hash}.html.gz
//   Example: example.com/a3f9c2d8e1b74f56.html.gz
//
// Content is stored gzip-compressed to save storage space.
// The content_hash is xxHash64 of the raw (uncompressed) body.
//
// DEDUP STRATEGY:
// Before storing, we check if the content_hash already exists in Redis:
//   Key: CONTENTHASH:{hash}  →  minio_key
//   TTL: 7 days
//
// If the hash exists → content hasn't changed → skip storage + processing.
// If not → store in MinIO → notify processor via Redis Stream.

package storage

import (
	"bytes"
	"compress/gzip"
	"context"
	"fmt"
	"hash/fnv"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
)

const (
	contentHashKeyPrefix = "CONTENTHASH:"
	contentHashTTL       = 7 * 24 * 60 * 60 // 7 days in seconds
)

// StorageClient wraps MinIO and Redis for content-addressed HTML storage.
type StorageClient struct {
	minio  *minio.Client
	redis  *redis.Client
	bucket string
}

// StorageResult is returned after a successful store operation.
type StorageResult struct {
	MinIOKey     string // the object key in MinIO
	ContentHash  string // hex hash of the raw content
	Bytes        int    // compressed size stored
	WasDuplicate bool   // true if content was already stored (unchanged)
}

// NewStorageClient creates a MinIO + Redis backed storage client.
func NewStorageClient(
	redisClient *redis.Client,
	endpoint, accessKey, secretKey, bucket string,
	secure bool,
) (*StorageClient, error) {
	mc, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: secure,
	})
	if err != nil {
		return nil, fmt.Errorf("create minio client: %w", err)
	}

	return &StorageClient{
		minio:  mc,
		redis:  redisClient,
		bucket: bucket,
	}, nil
}

// EnsureBucket creates the storage bucket if it does not exist.
// Call once at startup.
func (s *StorageClient) EnsureBucket(ctx context.Context) error {
	exists, err := s.minio.BucketExists(ctx, s.bucket)
	if err != nil {
		return fmt.Errorf("check bucket: %w", err)
	}
	if !exists {
		if err := s.minio.MakeBucket(ctx, s.bucket, minio.MakeBucketOptions{}); err != nil {
			return fmt.Errorf("create bucket: %w", err)
		}
	}
	return nil
}

// Store saves raw HTML to MinIO with content-addressed deduplication.
//
// Algorithm:
//  1. Compute content hash (FNV-64a of raw body)
//  2. Check Redis: has this hash been stored before?
//     YES → return existing MinIO key with WasDuplicate=true (skip processing)
//     NO  → gzip-compress body, upload to MinIO, record hash in Redis
//
// Args:
//
//	ctx:    Context for cancellation
//	domain: Site domain (used as path prefix in MinIO key)
//	body:   Raw HTML bytes (uncompressed)
//
// Returns StorageResult with MinIO key and content hash.
func (s *StorageClient) Store(ctx context.Context, domain string, body []byte) (*StorageResult, error) {
	// Step 1: Hash the raw content
	hash := contentHash(body)

	// Step 2: Check Redis dedup cache
	hashKey := contentHashKeyPrefix + hash
	existing, err := s.redis.Get(ctx, hashKey).Result()
	if err == nil && existing != "" {
		// Content hasn't changed — skip storage
		return &StorageResult{
			MinIOKey:     existing,
			ContentHash:  hash,
			Bytes:        0,
			WasDuplicate: true,
		}, nil
	}

	// Step 3: gzip-compress the content
	compressed, err := gzipCompress(body)
	if err != nil {
		return nil, fmt.Errorf("compress: %w", err)
	}

	// Step 4: Build the MinIO object key
	// Format: {domain}/{hash}.html.gz
	minioKey := fmt.Sprintf("%s/%s.html.gz", domain, hash)

	// Step 5: Upload to MinIO
	reader := bytes.NewReader(compressed)
	_, err = s.minio.PutObject(ctx, s.bucket, minioKey, reader, int64(len(compressed)),
		minio.PutObjectOptions{
			ContentType:     "text/html",
			ContentEncoding: "gzip",
			// Tag with domain for lifecycle policies
			UserMetadata: map[string]string{
				"domain":   domain,
				"raw-hash": hash,
			},
		},
	)
	if err != nil {
		return nil, fmt.Errorf("minio put: %w", err)
	}

	// Step 6: Record in Redis (so future identical crawls are detected)
	_ = s.redis.Set(ctx, hashKey, minioKey, contentHashTTL).Err()

	return &StorageResult{
		MinIOKey:     minioKey,
		ContentHash:  hash,
		Bytes:        len(compressed),
		WasDuplicate: false,
	}, nil
}

// GetRaw retrieves raw (compressed) HTML from MinIO and decompresses it.
// Used by the Processor service when it needs the full HTML.
func (s *StorageClient) GetRaw(ctx context.Context, minioKey string) ([]byte, error) {
	obj, err := s.minio.GetObject(ctx, s.bucket, minioKey, minio.GetObjectOptions{})
	if err != nil {
		return nil, fmt.Errorf("minio get: %w", err)
	}
	defer obj.Close()

	// Decompress
	reader, err := gzip.NewReader(obj)
	if err != nil {
		return nil, fmt.Errorf("gzip reader: %w", err)
	}
	defer reader.Close()

	var buf bytes.Buffer
	if _, err := buf.ReadFrom(reader); err != nil {
		return nil, fmt.Errorf("read decompressed: %w", err)
	}
	return buf.Bytes(), nil
}

// ContentHash computes a 16-hex-char FNV-64a hash of the content.
// Fast, non-cryptographic — perfect for dedup purposes.
func ContentHash(data []byte) string {
	h := fnv.New64a()
	h.Write(data)
	return fmt.Sprintf("%016x", h.Sum64())
}

// contentHash is the internal version, kept for compatibility.
func contentHash(data []byte) string {
	return ContentHash(data)
}

// gzipCompress compresses data using gzip at default compression level.
func gzipCompress(data []byte) ([]byte, error) {
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	if _, err := w.Write(data); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
