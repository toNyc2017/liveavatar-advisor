#!/usr/bin/env bash
# Skin-smoothing pipeline for avatar training videos.
#
# Two stages:
#   1. iPhone HDR/HEVC → SDR rec.709 H.264 conversion (skipped if input is
#      already SDR per ffprobe)
#   2. FFmpeg bilateral filter to even skin texture without blurring eyes,
#      lips, eyebrows
#
# This recipe produced TAKE_3_SDR_smooth_30.mp4 (the canonical Tom Olds
# training video). Strength 30 was chosen after a comparison sweep across
# 0, 20, 30, 40, 60 — see the "sweep" mode below to repeat the comparison
# on a new shoot if the lighting/skin tone is different enough to warrant
# re-picking.
#
# Usage:
#     ./scripts/smooth_video.sh smooth INPUT.MOV STRENGTH OUTPUT.mp4
#         Run the full pipeline once at the given strength.
#         Strength is the bilateral filter's sigmaS — higher = more smoothing.
#         Recommended: 30 (matches the canonical TAKE_3 output).
#
#     ./scripts/smooth_video.sh sweep INPUT.MOV
#         Generate variants at strengths 20/30/40/60 plus the raw baseline,
#         extract one frame from each at the 10-second mark, build a
#         side-by-side comparison grid (cal_<N>.jpg + comparison_grid.jpg).
#         Open comparison_grid.jpg and pick the strength that reads best.
#
# Examples:
#     ./scripts/smooth_video.sh smooth TAKE_4.MOV 30 TAKE_4_SDR_smooth_30.mp4
#     ./scripts/smooth_video.sh sweep TAKE_4.MOV
#
set -euo pipefail

mode="${1:-}"
input="${2:-}"

if [[ -z "$mode" || -z "$input" ]]; then
  sed -n '2,30p' "$0"
  exit 1
fi

if [[ ! -f "$input" ]]; then
  echo "ERROR: input file not found: $input" >&2
  exit 1
fi

# Detect whether the source is HDR (HLG / PQ) so we can skip the conversion
# stage when it isn't needed. iPhone HDR captures show up with
# color_transfer = arib-std-b67 (HLG) or smpte2084 (PQ).
detect_hdr() {
  local f="$1"
  local transfer
  transfer=$(ffprobe -v error -select_streams v:0 \
             -show_entries stream=color_transfer \
             -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null || true)
  case "$transfer" in
    arib-std-b67|smpte2084) return 0 ;;  # is HDR
    *) return 1 ;;
  esac
}

# Stage 1: HDR → SDR conversion (only when needed). Output is a new .mp4
# alongside the input. If the source is already SDR, just symlinks /
# copies the path so stage 2 has a uniform input name.
stage1_to_sdr() {
  local in="$1"
  local stem="${in%.*}"
  local sdr="${stem}_SDR.mp4"

  if detect_hdr "$in"; then
    echo "[stage 1] HDR detected — converting to SDR rec.709 H.264" >&2
    # The -color_* output flags rewrite the container metadata to match
    # the SDR rec.709 pixels the scale filter produces. Without these,
    # primaries / transfer tags inherit from the HDR source (bt2020 /
    # arib-std-b67) and downstream tools sometimes apply double tone
    # mapping on what they think is still HDR content.
    ffmpeg -y -i "$in" \
      -vf "scale=in_color_matrix=bt2020nc:in_range=tv:out_color_matrix=bt709:out_range=tv,format=yuv420p" \
      -vcodec libx264 -preset slow -crf 18 \
      -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv \
      -acodec aac -ar 48000 \
      -movflags +faststart \
      "$sdr"
  else
    echo "[stage 1] source is already SDR — skipping HDR→SDR conversion" >&2
    sdr="$in"
  fi
  printf '%s\n' "$sdr"
}

# Stage 2: bilateral smoothing at the requested strength.
# sigmaS scales with strength. sigmaR fixed at 0.1 to preserve edges
# (eyes, lips, eyebrows stay crisp while pores even out).
stage2_smooth() {
  local in="$1"
  local strength="$2"
  local out="$3"
  echo "[stage 2] bilateral sigmaS=${strength} sigmaR=0.1 → ${out}"
  # -color_* flags propagated through the bilateral re-encode so the
  # output stays cleanly tagged as SDR rec.709 (matches the stage-1 fix).
  ffmpeg -y -i "$in" \
    -vf "bilateral=sigmaS=${strength}:sigmaR=0.1" \
    -vcodec libx264 -preset slow -crf 18 \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv \
    -acodec copy \
    -movflags +faststart \
    "$out"
}

case "$mode" in
  smooth)
    strength="${3:-30}"
    out="${4:-${input%.*}_SDR_smooth_${strength}.mp4}"
    sdr=$(stage1_to_sdr "$input")
    stage2_smooth "$sdr" "$strength" "$out"
    echo
    echo "Done. Output: $out"
    ;;

  sweep)
    sdr=$(stage1_to_sdr "$input")
    stem="${sdr%.*}"
    echo
    echo "Generating variants..."
    # 0 = raw baseline (no filter), other strengths = bilateral
    for strength in 0 20 30 40 60; do
      variant="${stem}_smooth_$(printf '%02d' "$strength").mp4"
      if [[ "$strength" -eq 0 ]]; then
        # Copy the raw SDR for direct comparison
        cp -f "$sdr" "$variant"
      else
        ffmpeg -y -loglevel error -i "$sdr" \
          -vf "bilateral=sigmaS=${strength}:sigmaR=0.1" \
          -vcodec libx264 -preset fast -crf 20 -acodec copy \
          "$variant"
      fi
      echo "  ✓ ${variant}"
    done

    echo
    echo "Extracting comparison frames at t=10s..."
    for strength in 0 20 30 40 60; do
      variant="${stem}_smooth_$(printf '%02d' "$strength").mp4"
      ffmpeg -y -loglevel error -ss 10 -i "$variant" -frames:v 1 \
        "cal_$(printf '%02d' "$strength").jpg"
    done

    echo "Building side-by-side comparison grid..."
    ffmpeg -y -loglevel error \
      -i cal_00.jpg -i cal_20.jpg -i cal_30.jpg -i cal_40.jpg -i cal_60.jpg \
      -filter_complex "[0:v][1:v][2:v][3:v][4:v]hstack=inputs=5" \
      comparison_grid.jpg

    echo
    echo "Done. Open comparison_grid.jpg to pick the strength."
    echo "Then re-run with: ./scripts/smooth_video.sh smooth $input <strength> <output.mp4>"
    ;;

  *)
    echo "ERROR: unknown mode '$mode' (use 'smooth' or 'sweep')" >&2
    sed -n '2,30p' "$0"
    exit 1
    ;;
esac
