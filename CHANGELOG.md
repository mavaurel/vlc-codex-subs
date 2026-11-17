# Changelog

## 1.6.2

- Make the preprocessing cancellation test independent of the developer’s local
  Codex model configuration, so the full suite also runs on clean CI machines.

## 1.6.1

First public installation release.

- Source-only installation ZIP with SHA-256 checksum; optional DVD reader is
  built locally and never bundled as an architecture-specific binary.
- macOS `Install.command`, English and Russian installation guides, MIT license
  and third-party dependency notices.
- Fresh install, upgrade, uninstall and release-content regression checks.
- Automated Python/Lua tests on macOS and Linux.

## 1.6.0

- Local DVD folder discovery and bitmap subtitle extraction for a selected title.
- DVD OCR reuses image verification, translation, review and original-image fallback.
- Preserve title identity and chapter timings, including DVD discontinuities.

## 1.5.1

- Cancel stops preprocessing children and clears an abandoned OCR job's stale status.
- Preserve OCR evidence and reviewed checkpoints after cancellation.

## 1.5

- Per-video job history and recovery after switching videos.
- Save completed subtitles beside the video while preserving existing files.
- Up to 15 parallel translation/review batches, shared rate-limit backoff,
  reusable drafts and reviewed checkpoints.
- Conservative repair of uncertain captions; retain the original text or image
  when supplied evidence cannot establish a faithful Russian translation.
