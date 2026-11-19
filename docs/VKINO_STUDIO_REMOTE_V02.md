# VKino Studio 0.2 — Windows worker / VPS bridge (draft)

This is an opt-in transport layer in `feature/vkino-studio-windows-mvp`. It **does not** publish videos or change the existing render flow until deployed and called explicitly. Do not merge to `main` without a staging review.

## Architecture

- VKino VPS: existing `Studio` owner/session auth, PostgreSQL durable queue `studio_worker_jobs`, worker-only hashed tokens, upload/download of approved media, chunked transfer of MP4 drafts.
- Windows 10 PC: `python -m vkino_studio.remote` makes outbound HTTPS requests only. No incoming port, webhook, router forwarding, or shell commands from VPS.
- Manual approval remains mandatory: the new queue returns `awaiting_review`. There is **no** publication endpoint in the worker API. Existing YouTube API is untouched.
- The old local `vkino_studio` GUI and CLI remain available without a server connection.

## Rollout safety

1. Review new migration **15** and the new scoped API routes. Backup PostgreSQL. Existing migrations 1–14 must not change.
2. Deploy only to a staging VPS or with the new worker feature disabled by default. An unpaired worker does nothing. No automatic migration/deployment to production from this branch.
3. Test pairing and revocation with a non-public single-use code issued by admin command `/studio`.
4. Test uploading *author-approved* still image or video **plus a separate approved WAV or MP3 voice track**. Each asset is currently limited to **50 MB** by the Studio VPS library; outputs use 4 MiB chunks with a 256 MiB maximum.
5. Exercise claim → download → SHA-256 verification → render → chunked upload → `awaiting_review` → manual download; verify that no public post appears.
6. Confirm worker restart and lease-expiry handling. An expired job becomes `interrupted`; it is never automatically retried. Only the authenticated admin may explicitly retry after checking the result.
7. Before enabling for routine use: ensure Windows 10 has current extended security updates or use a supported OS; protect the worker token, secure disk, and backups.

## Windows prerequisites

- Python 3.12+ and FFmpeg/FFprobe in PATH, plus project requirements (Pillow). Tkinter for the standalone GUI.
- Recommended first pass: CPU `libx264`; GTX 1070 optional `--encoder h264_nvenc` after a manual encoder check.
- A stable HTTPS domain of your existing VKino bot with valid TLS certificate. Use the same Caddy reverse proxy; no new public service is needed.

## Pair the PC (after enabling server routes in a staging deploy)

1. In the **private chat** with VKino send `/studio`. Receive a one-use code (valid for 10 minutes).
2. On the Windows PC:

```powershell
py -m vkino_studio.remote --server https://YOUR_VKINO_DOMAIN --pair
```

3. Save the returned token as a secret local Windows user environment variable `VKINO_STUDIO_WORKER_TOKEN`; do not paste it in GitHub, logs, or Telegram. Tokens expire after 30 days. `/studio revoke` revokes worker tokens too.
4. Start the outgoing worker:

```powershell
py -m vkino_studio.remote --server https://YOUR_VKINO_DOMAIN --encoder libx264
```

Use `--once` for a one-job smoke test. No port needs opening on the home router.

## Administrator submission contract

The website's existing Studio session (NOT the worker token) uploads source and narration assets through `POST /api/studio/v1/files`. Accepted types include image, video, WAV/MP3 audio, up to 50 MB per file and 512 MB per owner in aggregate. The response has `id`, `hash`, `size`.

Submit to `POST /api/studio/v1/worker/jobs` with that **Studio session** token:

```json
{
  "requestId": "d0e647cb-2204-4aee-8278-ae42c7a671f4",
  "revision": 1,
  "authorApproved": true,
  "project": {
    "version": 1,
    "title": "Тест KinoMir360",
    "width": 720,
    "height": 1280,
    "fps": 25,
    "captions": true,
    "scenes": [{
      "source": "media/frame.jpg", "voice": "media/voice.wav",
      "text": "Тестовый выпуск", "headline": "КИНОНОВОСТИ",
      "duration": 3.0, "start": 0.0, "rights": "author", "approved": true
    }]
  },
  "assets": [
    {"path": "media/frame.jpg", "fileId": "UUID_FROM_UPLOAD", "sha256": "64_HEX_SHA256", "size": 1024},
    {"path": "media/voice.wav", "fileId": "UUID_FROM_UPLOAD", "sha256": "64_HEX_SHA256", "size": 51200}
  ]
}
```

Required: real UUIDs, exact hash/size values from uploaded material, unique `media/<filename>` paths, and explicit `approved: true` on each scene. Reusing the same `requestId` with a different payload is rejected. The worker cannot create jobs or upload arbitrary assets without explicit server approval.

- Author status: `GET /api/studio/v1/worker/jobs/{id}` with Studio session token.
- Draft result: `GET /api/studio/v1/worker/jobs/{id}/result` with Studio session token, only after `awaiting_review`.
- Explicit retry after failure: `POST /api/studio/v1/worker/jobs/{id}/retry` with Studio session token.
- Windows-only routes `pair`, `claim`, `heartbeat`, asset transfer, parts, `finish`, `fail` require the scoped worker token; claims also use a 15-minute rotating lease renewed by heartbeats.

## Not yet implemented

- An automatic button in the existing Site/Telegram flow to prepare a remote job from its existing scene/TTS references. The author currently prepares an approved job through the documented authenticated API. Integrating it into the UI is the next stage.
- Downloading large (>50 MB) source assets directly from the user's PC without staging them on the 2 GB VPS. This needs a separate authenticated staging/storage design, not an open arbitrary-URL downloader.
- Native Windows `.exe` installer, background Windows service and auto-update.
- AI semantic scene selection, fine-grained word alignment, intelligent editor memory, long-form video beyond 180 s.
- Real Windows hardware and production VPS integration tests. The local protocol and synthetic render tests do **not** prove production readiness.

## Security properties

- Server only hands out media IDs belonging to the authenticated Studio owner and present in an approved immutable job payload.
- Scoped worker token is stored hashed on the VPS; token is useless for Studio session or YouTube publication endpoints.
- Client rejects redirects, arbitrary download domains and invalid HTTPS endpoints; only reads the predefined worker API.
- Every input is hashed and size-checked, loaded under a fresh temp project directory; local manifest prevents file traversal.
- Output upload uses idempotent, SHA-256 checked 4 MiB parts. Total size capped at 256 MiB. VPS verifies video stream with `ffprobe`, then records a draft, never publishes.
- Worker failures do not cause paid work to be automatically re-run; manual admin retry only.
- The result on VPS is kept for review for up to 7 days; put approved final files in durable storage before cleanup.
