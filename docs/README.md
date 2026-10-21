# Documentation website

The homepage presents Luminesk 2.x at `https://nesk.farin.cc`, maintained by
Farin Works. Its installation commands and latest-release
links follow the same release workflow as the installation guide. Documentation
routes remain unchanged. The Docusaurus mobile sidebar is available only under
`/docs`; other pages use direct homepage navigation.

## Validate and preview

```bash
pnpm --dir docs install --frozen-lockfile
pnpm --dir docs audit:security
pnpm --dir docs check:images
pnpm --dir docs typecheck
pnpm --dir docs build
pnpm --dir docs serve --host 127.0.0.1 --port 4173 --no-open
```

The build also runs `scripts/check-landing.test.mjs` against the exported HTML.
`pnpm --dir docs check:landing` reruns those checks without rebuilding. They cover
semantic headings, metadata, canonical URLs, structured data, sitemap, robots,
installation content without JavaScript, social assets, and sidebar routing.

For browser QA, check widths from 320 to 1920 px, keyboard focus, the documentation
menu after client navigation, back-button scroll restoration, clipboard success
and denial, reduced motion, and native disclosures with JavaScript disabled.
Measure Lighthouse mobile against a server with production HTTP compression;
Docusaurus's local `serve` command sends uncompressed assets. Recheck the deployed
site after release because local results do not measure the production host.

Documentation CI already uploads a `documentation-site` artifact for branch/PR
review. Production publication remains controlled by the existing deploy workflow.
To request indexing after publication, submit `/sitemap.xml` through Google Search
Console and Yandex Webmaster. Access to those verified site accounts is required.
Static content, canonical links and sitemap help crawlers discover pages; they do
not guarantee inclusion or ranking. See [Google's JavaScript SEO guidance](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics)
and [Yandex's robots.txt guidance](https://yandex.com/support/webmaster/en/controlling-robot/robots-txt).

## Visual assets

The hero restores the large wordmark and black/rose atmosphere of the 1.1
homepage. `BrandLogo.tsx` preserves the original letter paths; `BrandAtmosphere.tsx`
draws moving light ribbons with SVG and CSS. The logo uses the original 1.1 GSAP
timeline: 0.3s initial delay, 0.4s letter strokes staggered by 0.08s, the dot at
0.44s and the CLI pill at 0.9s within the timeline. Its gradient loops every 5s.
No canvas, external fonts or raster
hero assets are required. Glass buttons and dark rose panels carry the visual
language through the rest of the page. The recipe workflow highlights its stages
in sequence; sections enter using the Web Animations API.

The hero's Pause motion button freezes decorative animation across the page.
Replay intro redraws the wordmark. IntersectionObserver pauses scenes outside
the viewport, and hidden tabs pause automatically. Reduced motion disables
animation, including when the preference changes while the page is open.
Without JavaScript, the complete wordmark and content stay visible, motion
controls stay hidden, and native disclosures work.

`static/img/social-card.png` is a 1200 × 630 browser-rendered composition of the
same wordmark, vector ribbons and real text.
