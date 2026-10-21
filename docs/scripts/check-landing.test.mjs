import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const build =
  process.env.DOCS_BUILD_DIR ??
  fileURLToPath(new URL("../build/", import.meta.url));
const html = await readFile(join(build, "index.html"), "utf8");
const origin = "https://nesk.farin.cc";
const tags = (name) =>
  [...html.matchAll(new RegExp(`<${name}\\b[^>]*>`, "g"))].map(([tag]) => tag);
const attribute = (tag, name) =>
  tag.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1];
const meta = (name) =>
  tags("meta")
    .filter(
      (tag) =>
        attribute(tag, "name") === name || attribute(tag, "property") === name,
    )
    .map((tag) => attribute(tag, "content"));

test("exports a semantic, crawlable Minecraft homepage", () => {
  assert.equal(tags("h1").length, 1);
  assert.match(html, /<h1\b[^>]*>Minecraft servers\./);
  assert.match(html, /<title\b[^>]*>[^<]*Minecraft[^<]*Java[^<]*Bedrock/);
  assert.equal(meta("description").length, 1);
  assert.match(meta("description")[0], /Java and Bedrock/);
  assert.equal(
    tags("link")
      .filter((tag) => attribute(tag, "rel") === "canonical")
      .map((tag) => attribute(tag, "href"))
      .join(),
    `${origin}/`,
  );
  assert.equal(meta("og:url").join(), `${origin}/`);
  assert.equal(meta("og:image").join(), `${origin}/img/social-card.png`);
  assert.equal(meta("twitter:card").join(), "summary_large_image");
  assert.doesNotMatch(meta("robots").join(), /noindex/);
  const data = [
    ...html.matchAll(
      /<script\b[^>]*type="application\/ld\+json"[^>]*>(.*?)<\/script>/gs,
    ),
  ].map(([, text]) => JSON.parse(text));
  assert.ok(
    data.some(
      (entry) => entry["@type"] === "WebSite" && entry.url === `${origin}/`,
    ),
  );
  assert.ok(
    data.some(
      (entry) =>
        entry["@type"] === "SoftwareApplication" &&
        /Bedrock/.test(entry.description),
    ),
  );
});

test("renders installation and product content without JavaScript or user-agent detection", () => {
  for (const id of ["how-it-works", "recipes", "security", "installation"])
    assert.ok(html.includes(`id="${id}"`), id);
  for (const content of [
    "luminesk.toml",
    "luminesk.lock",
    ".lumineskpkg",
    "Docker runtime",
    "uv tool install luminesk-cli",
    "Python 3.13+",
    "No Python required",
    "no in-place upgrade",
  ])
    assert.ok(html.includes(content), content);
  assert.match(html, /href="\/docs\/migrating-to-2\.0"/);
  assert.match(html, /<details\b[^>]*open/);
  assert.doesNotMatch(html, /<canvas\b|gsap-fade-up/);
  for (const os of ["linux", "macos", "windows"])
    for (const arch of os === "windows" ? ["amd64"] : ["amd64", "arm64"])
      assert.ok(html.includes(`luminesk-${os}-${arch}.zip`));
  assert.ok(!html.includes("luminesk-windows-arm64.zip"));
});

test("keeps the documentation menu inside documentation routes", async () => {
  assert.doesNotMatch(html, /navbar__toggle|navbar-sidebar/);
  assert.match(html, /aria-label="Main navigation"/);
  assert.match(html, /href="\/docs"/);
  const docs = await readFile(join(build, "docs", "index.html"), "utf8");
  assert.match(docs, /navbar__toggle/);
});

test("ships indexable sitemap, robot rules, social image and a noindex error page", async () => {
  const robots = await readFile(join(build, "robots.txt"), "utf8");
  assert.match(robots, /User-agent: \*/);
  assert.match(robots, /Allow: \//);
  assert.ok(robots.includes(`Sitemap: ${origin}/sitemap.xml`));
  const sitemap = await readFile(join(build, "sitemap.xml"), "utf8");
  for (const path of [
    "/",
    "/docs/installation",
    "/docs/quick-start",
    "/docs/migrating-to-2.0",
  ])
    assert.ok(sitemap.includes(`<loc>${origin}${path}</loc>`), path);
  assert.doesNotMatch(sitemap, /404\.html/);
  const notFound = await readFile(join(build, "404.html"), "utf8");
  assert.match(notFound, /name="robots" content="noindex, follow"/);
  const social = await readFile(join(build, "img/social-card.png"));
  assert.equal(social.subarray(1, 4).toString(), "PNG");
  assert.equal(social.readUInt32BE(16), 1200);
  assert.equal(social.readUInt32BE(20), 630);
  assert.ok((await stat(join(build, "img/social-card.png"))).size < 500_000);
});

test("keeps the brand and motion controls accessible in the static hero", () => {
  const brand = tags("svg").filter(
    (tag) => attribute(tag, "role") === "img" && attribute(tag, "aria-label") === "Luminesk-CLI",
  );
  assert.equal(brand.length, 1);
  assert.ok(html.includes("Replay intro"));
  assert.ok(html.includes("Pause motion"));
  assert.ok(tags("button").some((tag) => attribute(tag, "aria-pressed") === "false"));
  assert.ok(html.includes("From recipe to running server"));
});

test("exports named edition choices with decorative icons and usable commands", () => {
  for (const edition of ["java", "bedrock"]) {
    const article = html.match(new RegExp(
      `<article\\b[^>]*aria-labelledby="${edition}-edition-title"[^>]*>(.*?)</article>`, "s",
    ))?.[1];
    assert.ok(article, `${edition} has a named article`);
    assert.match(article, new RegExp(`<h3\\b[^>]*id="${edition}-edition-title"`));
    assert.match(article, /<span\b[^>]*aria-hidden="true"[^>]*><svg\b/);
    assert.ok(article.includes(`<code>nesk search --edition ${edition}</code>`));
    assert.match(article, /<a\b[^>]*href="[^\"]+"/);
  }
});

test("links the main Lumi walkthrough from the Bedrock edition", () => {
  const bedrock = html.match(
    /<article\b[^>]*aria-labelledby="bedrock-edition-title"[^>]*>(.*?)<\/article>/s,
  )?.[1];
  assert.ok(bedrock);
  assert.match(bedrock, /Start with the Lumi walkthrough/);
  assert.match(bedrock, /href="\/docs\/quick-start"[^>]*>Try the Lumi recipe/);
  const java = html.match(
    /<article\b[^>]*aria-labelledby="java-edition-title"[^>]*>(.*?)<\/article>/s,
  )?.[1];
  assert.ok(java);
  assert.doesNotMatch(java, /href="\/docs\/quick-start"/);
});

test("shows copyright and the project license in homepage and documentation footers", async () => {
  const docs = await readFile(join(build, "docs", "index.html"), "utf8");
  for (const page of [html, docs]) {
    const footer = page.match(/<footer\b[^>]*>(.*?)<\/footer>/s)?.[1];
    assert.ok(footer, "footer is exported without JavaScript");
    const text = footer.replace(/<[^>]*>/g, "");
    assert.match(text, /© \d{4} Farin Works\. All rights reserved\./);
    assert.match(text, /GNU GPLv3 or later/);
    assert.ok(footer.includes('href="https://github.com/farin-works/luminesk-cli/blob/main/LICENSE"'));
    assert.doesNotMatch(text, /Reproducible from the first block|Built with Docusaurus/);
  }
});
