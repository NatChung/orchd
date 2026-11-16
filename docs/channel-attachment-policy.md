# Channel attachment policy

Attachment MIME types are taken from the provider metadata. Legacy entries without a MIME type use the filename extension and unknown types are rejected. The MIME type and filename are untrusted claims, not proof of content safety.

Set a per-source allowlist in `email/config.toml` under the channel home:

```toml
[[sources]]
id = "example"
attachment_mime_allowlist = ["text/plain", "application/pdf", "application/zip"]
```

Without an override, the allowlist is text/plain, text/csv, application/pdf, application/zip, image/png, image/jpeg, image/gif, image/webp, image/heic, video/mp4, video/quicktime, video/webm, audio/mp4, audio/m4a, audio/x-m4a, audio/mp3, audio/mpeg, audio/wav, audio/x-wav, audio/ogg and audio/webm. All types share the 25 MiB limit; no separate media limit is configured. An empty list rejects all types. ZIP content is inspected even when its claimed type is different: expanded content is limited to 20 MiB and 100 times the archive size; encrypted, corrupt, nested or unsupported compressed containers are rejected. Rejections are recorded as unavailable with a reason code. Size and digest checks still apply. Invalid configuration stops the sync before its mail checkpoint advances.
