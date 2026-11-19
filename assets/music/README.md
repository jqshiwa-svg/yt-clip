# Background music library

render.py only plays tracks from this folder, rotating through them (or the one the Editor names). With no tracks here, videos render without music.

Only add music you are licensed to use in monetised YouTube videos:

- **YouTube Audio Library** (studio.youtube.com, then Audio Library): free, cleared for YouTube. Filter for "No attribution required" so you don't need to add a credit line. This is the safest choice.
- **Paid libraries** (Epidemic Sound, Artlist, Uppbeat Premium): whitelist your channel in their dashboard so Content ID doesn't claim your videos.
- **Avoid** popular songs, "no copyright" uploads on YouTube, and TikTok sounds. These get claimed, and a claim stops the pipeline from publishing.

Keep a note of where each track came from in `LICENSES.md` next to the files. The music files and that note are gitignored, so they never end up in the public repo.
