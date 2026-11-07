# Diagram sources

`wp-diagram1.html` is the HTML source for `../wp-diagram1.png`, rendered with
headless Chrome. Regenerate after editing:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --hide-scrollbars \
  --screenshot=../wp-diagram1.png --window-size=1376,768 \
  --default-background-color=FFFFFF --force-device-scale-factor=2 \
  wp-diagram1.html
```

`--force-device-scale-factor=2` renders at 2× (2752×768→1536) for print sharpness;
the PDF CSS sizes images by `max-width:100%`, so pixel dimensions affect quality,
not layout.

**Diagrams 2–7 have no committed source** — they were image-generated on
2026-07-17 (see `git log 5ea7084`). If one of them needs a correction, prefer
rebuilding it in HTML like diagram 1 rather than re-prompting an image model:
the labels are load-bearing technical claims and a text-rendering model will
garble them. Diagram 1 was rebuilt this way on 2026-07-18 precisely because its
generated labels ("One governed exit. Nothing else leaves.") had frozen the
pre-correction single-lane egress framing into a file nobody could edit.
