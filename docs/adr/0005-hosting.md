# 0005 Hosting

Date: 2026-11-18. Status: accepted.

**Decision.** Render free web service, Docker runtime, blueprint in `render.yaml`. 512 MB, shared CPU,
sleeps after ~15 minutes idle. The UI polls `/readyz` and shows a "waking up" state; the KB and model
are baked into the image, so waking is a container start, not a crawl.

**Alternatives.** Google Cloud Run (scale to zero, needs a card on the account), Oracle Always Free
VM (always on, more setup), Koyeb or Railway (free allowances change often). Hugging Face Docker Spaces
and Fly.io were reported as no longer free in 2026 and were not relied on.

The image is a plain container that listens on `$PORT`, so moving hosts is a config change.
