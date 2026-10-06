# Navren 标志生成记录

工具：image_gen.imagegen。以用户确认的组合标志为参考，分别生成图标和文字标志，透明背景。

- navren-icon.png：1254 × 1254 像素，RGBA PNG。
- navren-wordmark.png：2172 × 724 像素，RGBA PNG。

以下为本次生成使用的完整提示词记录：

```json
[
  {
    "key": "navren-icon",
    "prompt": "Use case: background-extraction / precise-object-edit.\nImage 1 is the exact FINAL APPROVED Navren logo. Produce the standalone ICON ONLY as a production-ready transparent PNG.\nThis is careful separation and faithful reconstruction of the APPROVED asset, NOT a new creative design. Extract ONLY the two-blue abstract icon from the upper part of Image 1. Remove the word \"Navren\" completely and remove all of the light background. Preserve the exact two flowing asymmetric blue-plane silhouettes, their relative scale, their orientations, the exact curved white passage between them, the blunt angled terminal cuts, and the approved cobalt/ink-blue colors and finish. Do not rotate, mirror, redraw into a different shape, change the gap width, add new components or alter its optical proportions. Do not add letters.\nComposition: a SQUARE 1024 x 1024 pixel image with a genuinely TRANSPARENT alpha background. Uniformly scale the original complete icon up to approximately 84% of canvas width, preserving aspect ratio, centered optically with generous safe padding on all sides and no clipping. The empty passage must also be transparent, not painted white.\nHigh-resolution, clean smooth antialiased edges, no new shadows, no fake checkerboard pixels, no solid white panel, no app tile, no border, no labels and no text anywhere. Preserve the original mark exactly; only isolate it, remove the background and size it appropriately."
  },
  {
    "key": "navren-wordmark",
    "prompt": "Use case: background-extraction / precise-object-edit.\nImage 1 contains the exact FINAL APPROVED Navren logo. Produce the standalone WORDMARK ONLY as a production-ready transparent PNG.\nThis is faithful separation/reconstruction of the existing approved lettering, NOT new typography. Extract ONLY the exact word \"Navren\" from the bottom of Image 1. Remove the two-blue graphic symbol completely and remove the light background. Preserve the original approved letterforms, light-to-regular stroke weight, elegant smooth curves, the shape of capital N and all lowercase a-v-r-e-n, inter-letter spacing, horizontal alignment, proportions and deep ink-blue color. Do not substitute a different font, make the letters bolder, add serifs, alter kerning, uppercase the entire word, invent a slogan or add an icon. The exact spelling and case are \"Navren\".\nComposition: a WIDE HORIZONTAL landscape image, target 1536 x 512 pixels / 3:1 canvas aspect ratio, with genuinely TRANSPARENT alpha background. The unchanged wordmark should be uniformly enlarged to approximately 88% of the canvas width and centered vertically and horizontally; preserve its natural original width/height proportions with clean safe transparent margins. No clipping of any letter.\nSharp high-resolution lettering and clean smooth antialiased edges. The counters inside letters must be transparent. No fake checkerboard pixels, no opaque white rectangle, no shadow, no textured surface, no borders, no graphic symbol and no other text. Preserve the exact approved wordmark; only isolate it, remove the background and size it appropriately."
  }
]
```

