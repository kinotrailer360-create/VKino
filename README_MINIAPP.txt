VKino360 Mini App + Auto Sync

New environment variables:
WEBAPP_URL=https://your-railway-domain
VIDEO_PROVIDER_API_URL=https://legal-provider.example/api/catalog   (optional)
VIDEO_PROVIDER_API_TOKEN=...                                       (optional)
VIDEO_PROVIDER_SYNC_SECONDS=900                                    (optional)

Provider JSON schema example:
{
  "items": [
    {
      "kinopoisk_id": 12345,
      "season": null,
      "episode": null,
      "voice": "Official dub",
      "quality": 1080,
      "url": "https://cdn.example/video/master.m3u8",
      "type": "hls",
      "active": true
    }
  ]
}

Use only video sources that you are authorized to distribute.
