#!/usr/bin/env bash
# Build the ChromaDB cold-start seed tarball from the local chroma_db/
# folder and upload it to the seed bucket so App Runner can hydrate it
# on cold start.
#
# Run this:
#   * the first time after creating the apprunner.yaml stack, OR
#   * after re-ingesting the annuity corpus (chroma_db is now newer than
#     what's in S3).
#
# Local prereqs:
#   * chroma_db/ exists and is populated (run scripts/ingest.py first)
#   * AWS credentials in your environment (e.g. AWS_PROFILE or env vars)
#   * tar, gzip, aws cli
#
# Usage:
#   ./scripts/seed_chroma_s3.sh                       # auto-detect bucket from stack
#   ./scripts/seed_chroma_s3.sh BUCKET                # explicit bucket
#   ./scripts/seed_chroma_s3.sh BUCKET KEY            # explicit bucket + key

set -euo pipefail

cd "$(dirname "$0")/.."

CHROMA_DIR="chroma_db"
STACK_NAME="${STACK_NAME:-liveavatar-advisor}"
KEY_DEFAULT="chroma_db.tar.gz"

if [ ! -d "$CHROMA_DIR" ] || [ -z "$(ls -A "$CHROMA_DIR" 2>/dev/null)" ]; then
  echo "ERROR: $CHROMA_DIR/ is missing or empty." >&2
  echo "Run 'python scripts/ingest.py' first to build it." >&2
  exit 1
fi

BUCKET="${1:-}"
KEY="${2:-$KEY_DEFAULT}"

if [ -z "$BUCKET" ]; then
  echo "No bucket arg; auto-detecting from CloudFormation stack '$STACK_NAME'..."
  BUCKET=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='ChromaSeedBucketName'].OutputValue" \
    --output text)
  if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
    echo "ERROR: Could not auto-detect ChromaSeedBucketName from stack '$STACK_NAME'." >&2
    echo "Pass the bucket name as the first argument." >&2
    exit 1
  fi
fi

echo "Bucket: $BUCKET"
echo "Key:    $KEY"

TMP_TARBALL=$(mktemp -t chroma_db.XXXXXX.tar.gz)
trap 'rm -f "$TMP_TARBALL"' EXIT

echo "Tarring $CHROMA_DIR/ → $TMP_TARBALL ..."
tar -czf "$TMP_TARBALL" "$CHROMA_DIR"

SIZE=$(du -h "$TMP_TARBALL" | cut -f1)
echo "Archive size: $SIZE"

echo "Uploading to s3://$BUCKET/$KEY ..."
aws s3 cp "$TMP_TARBALL" "s3://$BUCKET/$KEY" \
  --metadata "source-commit=$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown'),uploaded-at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo
echo "Done."
echo "App Runner will hydrate from this on next cold start."
echo "To force an immediate redeploy:"
echo "  aws apprunner start-deployment --service-arn \$(aws cloudformation describe-stacks --stack-name $STACK_NAME --query \"Stacks[0].Outputs[?OutputKey=='AppRunnerServiceArn'].OutputValue\" --output text)"
