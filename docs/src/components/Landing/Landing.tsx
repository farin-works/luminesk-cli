import React, {
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react';
import Head from '@docusaurus/Head';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import {
  ArrowRight,
  ArrowUpRight,
  Cube,
  Coffee,
  Fingerprint,
  GitBranch,
  ShieldCheck,
  FileCode,
  LockKey,
  Play,
  Pause,
  ArrowCounterClockwise,
  ArrowDown,
} from '@phosphor-icons/react';
import Installation from './Installation';
import BrandLogo from './BrandLogo';
import BrandAtmosphere from './BrandAtmosphere';
import useLandingMotion from './useLandingMotion';
import styles from './Landing.module.css';

const repository = 'https://github.com/farin-works/luminesk-cli';
const catalog = 'https://github.com/farin-works/luminesk-database';
const workflow = [
  {
    title: 'Recipe',
    icon: FileCode,
    file: 'luminesk.toml',
    description: 'Declare sources, settings, ownership and the Docker runtime.',
    href: '/docs/manifest-reference',
  },
  {
    title: 'Lock',
    icon: LockKey,
    file: 'luminesk.lock',
    description:
      'Resolve exact source hashes, recipe revisions and image digests.',
    href: '/docs/manifest-and-lockfile',
  },
  {
    title: 'Package',
    icon: Cube,
    file: '.lumineskpkg',
    description:
      'Build a deterministic package. Verify it before applying the plan.',
    href: '/docs/lockfile-and-packages',
  },
  {
    title: 'Run',
    icon: Play,
    file: 'Docker runtime',
    description:
      'Start the locked image and wait for the declared readiness checks.',
    href: '/docs/runtime-and-docker',
  },
];
const examples = [
  {
    title: 'Recipe',
    filename: 'luminesk.toml',
    description:
      'Excerpt from the recipe authoring guide. The manifest is yours to review; Luminesk generates the lock.',
    code: '[ownership]\npreserve = ["settings.yml"]\ndata = [".tmp", "worlds", "plugins", "players", "behavior_packs", "resource_packs"]\n\n[update]\nstrategy = "transactional"\nbackup = ["worlds", "plugins", "players", "behavior_packs", "resource_packs", "settings.yml"]\nretain_backups = 3\nrollback_on_failure = true',
    link: '/docs/creating-a-recipe',
    label: 'Read the complete recipe',
  },
  {
    title: 'Lock',
    filename: 'Resolve the recipe',
    description:
      'Run in your recipe source directory. Resolution binds mutable inputs to exact content and the current platform.',
    code: 'nesk validate --dir ./my-lumi-core --static\nnesk lock --dir ./my-lumi-core\nnesk cache verify',
    link: '/docs/lockfile-and-packages',
    label: 'Understand the lockfile',
  },
  {
    title: 'Plan',
    filename: 'Preview an update',
    description:
      'For an installed instance, inspect local changes and preview the update before applying it.',
    code: 'nesk diff --dir ./servers/example\nnesk update --dir ./servers/example --dry-run',
    link: '/docs/updating-instances',
    label: 'Explore transactional updates',
  },
  {
    title: 'JSON',
    filename: 'Automate without prompts',
    description:
      'Use structured results and explicit non-interactive behavior in your scripts. Missing required inputs return an error.',
    code: 'nesk status --dir ./servers/example --json --non-interactive',
    link: '/docs/command-reference',
    label: 'Read the CLI contract',
  },
];

export default function Landing(): ReactNode {
  const root = useRef<HTMLElement>(null);
  const [paused, setPaused] = useState(false);
  const [intro, setIntro] = useState(0);
  useLandingMotion(root, paused);
  return (
    <Layout
      title="Reproducible Minecraft servers for Java & Bedrock"
      description="Compose Minecraft Java and Bedrock servers with Luminesk-CLI. Lock recipe inputs, deploy verified packages in Docker, and preview updates with rollback."
      wrapperClassName={styles.layout}
      noFooter
    >
      <Head>
        <meta property="og:type" content="website" />
        <meta
          property="og:image:alt"
          content="Luminesk-CLI: reproducible Minecraft servers for Java and Bedrock"
        />
        <meta
          name="twitter:image:alt"
          content="Luminesk-CLI: reproducible Minecraft servers for Java and Bedrock"
        />
        <script type="application/ld+json">
          {JSON.stringify({
            '@context': 'https://schema.org',
            '@type': 'SoftwareApplication',
            name: 'Luminesk-CLI',
            applicationCategory: 'DeveloperApplication',
            operatingSystem: 'Linux, macOS, Windows',
            url: 'https://nesk.farin.cc/',
            description:
              'Reproducible Minecraft Java and Bedrock server recipe composer and lifecycle manager with Docker.',
            license: 'https://www.gnu.org/licenses/gpl-3.0.html',
          })}
        </script>
      </Head>
      <main ref={root} className={styles.landing}>
        <section
          className={styles.hero}
          aria-labelledby="hero-title"
          data-motion-scene="hero"
        >
          <BrandAtmosphere />
          <div className={`${styles.container} ${styles.heroCopy}`}>
            <BrandLogo key={intro} />
            <h1 id="hero-title">
              Minecraft servers. <span>Java &amp; Bedrock.</span>
            </h1>
            <p className={styles.heroDescription}>
              Compose recipes, lock inputs and manage your servers in Docker.
            </p>
            <div className={styles.actions}>
              <a href="#installation" className={styles.primaryButton}>
                Install Luminesk <ArrowRight aria-hidden="true" />
              </a>
              <Link to="/docs/quick-start" className={styles.secondaryButton}>
                Get started <ArrowUpRight aria-hidden="true" />
              </Link>
            </div>
          </div>
          <div className={`${styles.container} ${styles.heroBottom}`}>
            <a href="#how-it-works" className={styles.scrollCue}>
              <ArrowDown aria-hidden="true" /> From recipe to running server
            </a>
            <div className={styles.motionControls}>
              <button
                type="button"
                disabled={paused}
                onClick={() => setIntro((value) => value + 1)}
              >
                <ArrowCounterClockwise aria-hidden="true" /> Replay intro
              </button>
              <button
                type="button"
                aria-pressed={paused}
                onClick={() => setPaused((value) => !value)}
              >
                {paused ? (
                  <Play aria-hidden="true" />
                ) : (
                  <Pause aria-hidden="true" />
                )}
                {paused ? 'Resume motion' : 'Pause motion'}
              </button>
            </div>
          </div>
        </section>
        <div
          className={`${styles.container} ${styles.proof}`}
          aria-label="Platform and verification"
        >
          <span>Java + Bedrock</span>
          <span>Linux / macOS / Windows</span>
          <span>AMD64 / ARM64*</span>
          <span>SHA-256 verified</span>
          <span>Docker runtime</span>
        </div>
        <section
          id="how-it-works"
          className={`${styles.container} ${styles.section}`}
          aria-labelledby="workflow-title"
          data-motion-scene="workflow"
        >
          <h2 id="workflow-title" data-reveal>
            From recipe to server.
          </h2>
          <p className={styles.sectionIntro}>
            Catalog installs handle these four steps for you.
          </p>
          <ol className={styles.workflow}>
            {workflow.map((step, index) => (
              <li
                key={step.title}
                data-reveal
                style={{ '--step': index } as CSSProperties}
              >
                <span className={styles.stepIcon}>
                  <step.icon aria-hidden="true" size={27} />
                </span>
                <Link to={step.href} className={styles.stepTitle}>
                  {step.title}
                  <ArrowRight aria-hidden="true" />
                </Link>
                <code>{step.file}</code>
                <p>{step.description}</p>
              </li>
            ))}
          </ol>
        </section>
        <section
          className={`${styles.container} ${styles.explorerSection}`}
          aria-labelledby="explorer-title"
          data-motion-scene="explorer"
        >
          <div className={styles.explorerIntro}>
            <h2 id="explorer-title" data-reveal>
              Review your configuration.
            </h2>
            <p>
              Sources, files, runtime and updates live in an explicit contract.
              Inspect it before it touches your instance.
            </p>
            <Link to="/docs/manifest-and-lockfile" className={styles.textLink}>
              Explore the format <ArrowUpRight aria-hidden="true" />
            </Link>
          </div>
          <div className={styles.explorer}>
            {examples.map((example, index) => (
              <details
                key={example.title}
                name="product-example"
                open={index === 0}
              >
                <summary>
                  <span>{example.title}</span>
                  <span className={styles.exampleFilename}>
                    {example.filename}
                  </span>
                </summary>
                <div className={styles.exampleBody}>
                  <p>{example.description}</p>
                  <pre tabIndex={0} aria-label={`${example.title} example`}>
                    <code>{example.code}</code>
                  </pre>
                  <Link to={example.link}>
                    {example.label} <ArrowUpRight aria-hidden="true" />
                  </Link>
                </div>
              </details>
            ))}
          </div>
        </section>
        <section
          className={`${styles.container} ${styles.section}`}
          aria-labelledby="benefits-title"
          data-motion-scene="benefits"
        >
          <h2 id="benefits-title" data-reveal>
            Updates and file ownership.
          </h2>
          <div className={styles.benefits}>
            <article className={styles.ownershipFeature} data-reveal>
              <GitBranch size={30} aria-hidden="true" />
              <h3>File ownership</h3>
              <p>
                Ownership rules separate recipe files from local configuration
                and player data. Updates stop at conflicts instead of
                overwriting local drift.
              </p>
              <div className={styles.fileOwnership}>
                <span>
                  server.jar <code>managed</code>
                </span>
                <span>
                  eula.txt <code>generated</code>
                </span>
                <span>
                  server.properties <code>preserve</code>
                </span>
                <span>
                  world/ <code>data</code>
                </span>
              </div>
              <Link to="/docs/ownership">
                Understand file ownership <ArrowUpRight aria-hidden="true" />
              </Link>
            </article>
            <article className={styles.rollbackFeature} data-reveal>
              <h3>Transactional updates</h3>
              <p>
                Preview updates. Apply a transaction. If apply or readiness
                fails, restore the previous state when possible.
              </p>
              <div className={styles.rollbackFlow}>
                <span>Preview</span>
                <ArrowRight aria-hidden="true" />
                <span>Apply</span>
                <ArrowRight aria-hidden="true" />
                <span>Check</span>
              </div>
              <p className={styles.rollbackNote}>Failed check → rollback</p>
              <Link to="/docs/updating-instances">
                See how recovery works <ArrowUpRight aria-hidden="true" />
              </Link>
            </article>
            <article className={styles.automationFeature} data-reveal>
              <div>
                <h3>JSON automation</h3>
                <p>
                  Stable JSON, explicit inputs and non-interactive commands for
                  repeatable automation.
                </p>
              </div>
              <pre tabIndex={0} aria-label="JSON status command">
                <code>nesk status --json --non-interactive</code>
              </pre>
            </article>
          </div>
        </section>
        <section
          id="recipes"
          className={`${styles.container} ${styles.section}`}
          aria-labelledby="recipes-title"
          data-motion-scene="recipes"
        >
          <h2 id="recipes-title" data-reveal>
            Java &amp; Bedrock recipes.
          </h2>
          <p className={styles.sectionIntro}>
            Discover cores in the official catalog or compose a recipe of your
            own. Available versions and platforms depend on the recipe.
          </p>
          <div className={styles.editions}>
            <article aria-labelledby="java-edition-title">
              <div className={styles.editionHeading}>
                <span className={styles.editionMark} aria-hidden="true">
                  <Coffee size={32} weight="duotone" />
                </span>
                <h3 id="java-edition-title">Java Edition</h3>
              </div>
              <p>
                Choose a Java recipe and inspect its inputs, sources and
                platform requirements.
              </p>
              <code>nesk search --edition java</code>
              <a href={`${catalog}/tree/main/database`}>
                Browse catalog recipes <ArrowUpRight aria-hidden="true" />
              </a>
            </article>
            <article aria-labelledby="bedrock-edition-title">
              <div className={styles.editionHeading}>
                <span className={styles.editionMark} aria-hidden="true">
                  <Cube size={32} weight="duotone" />
                </span>
                <h3 id="bedrock-edition-title">Bedrock Edition</h3>
              </div>
              <p>
                Start with the Lumi walkthrough. Find other Bedrock cores in
                the catalog.
              </p>
              <code>nesk search --edition bedrock</code>
              <Link to="/docs/quick-start">
                Try the Lumi recipe <ArrowUpRight aria-hidden="true" />
              </Link>
            </article>
          </div>
          <p className={styles.platformNote}>
            * Linux/macOS bundles target AMD64 and ARM64; Windows targets AMD64.
            Each server recipe and Docker
            image must support your target platform.
          </p>
          <div className={styles.providerLinks}>
            <span>Bring your sources</span>
            {[
              ['HTTP', 'http'],
              ['Maven', 'maven'],
              ['Jenkins', 'jenkins'],
              ['GitHub', 'github-release'],
              ['GitLab', 'gitlab-release'],
              ['Mojang', 'mojang-version'],
              ['Paper', 'paper'],
              ['Local files', 'local-file'],
            ].map(([label, anchor]) => (
              <Link key={anchor} to={`/docs/sources#${anchor}`}>
                {label}
              </Link>
            ))}
          </div>
        </section>
        <section
          id="security"
          className={styles.securitySection}
          aria-labelledby="security-title"
          data-motion-scene="security"
        >
          <div className={styles.container}>
            <h2 id="security-title" data-reveal>
              Verification and isolation.
            </h2>
            <div className={styles.securityGrid}>
              <article>
                <Fingerprint size={32} aria-hidden="true" />
                <h3>Exact content</h3>
                <p>
                  SHA-256 checks for remote bytes. Immutable OCI image digests.
                  Deterministic packages verified before use.
                </p>
                <Link to="/docs/lockfile-and-packages">
                  Verification model <ArrowUpRight aria-hidden="true" />
                </Link>
              </article>
              <article>
                <ShieldCheck size={32} aria-hidden="true" />
                <h3>Bounded operations</h3>
                <p>
                  Explicit download limits, revalidated redirects and safe
                  archive paths keep external inputs within defined boundaries.
                </p>
                <Link to="/docs/sources">
                  Source guarantees <ArrowUpRight aria-hidden="true" />
                </Link>
              </article>
              <article>
                <Cube size={32} aria-hidden="true" />
                <h3>Explicit isolation</h3>
                <p>
                  Docker runs argument arrays with a read-only root by default.
                  Recipes declare mounts, ports and resource limits.
                </p>
                <Link to="/docs/runtime-and-docker">
                  Runtime boundary <ArrowUpRight aria-hidden="true" />
                </Link>
              </article>
            </div>
            <p className={styles.securityFootnote}>
              Review release checksums, SBOM and build provenance in{' '}
              <a href={`${repository}/releases`}>GitHub Releases</a>.
              Transaction rollback complements independent world backups.
            </p>
          </div>
        </section>
        <aside
          className={`${styles.container} ${styles.migration}`}
          aria-labelledby="migration-title"
        >
          <div>
            <h2 id="migration-title">Coming from Luminesk 1.x?</h2>
            <p>
              2.0 uses new recipes, locks and instance state. There is no
              in-place upgrade. Install side by side, then migrate user-owned
              data.
            </p>
          </div>
          <Link to="/docs/migrating-to-2.0">
            Read the migration guide <ArrowUpRight aria-hidden="true" />
          </Link>
        </aside>
        <Installation />
      </main>
      <footer className={`${styles.container} ${styles.footer}`}>
        <div className={styles.footerTop}>
          <Link
            to="/"
            className={styles.footerBrand}
            aria-label="Luminesk-CLI home"
          >
            <img
              src="/img/logo.svg"
              alt="Luminesk-CLI"
              width="130"
              height="26"
            />
          </Link>
          <nav aria-label="Footer navigation">
            <Link to="/docs">Documentation</Link>
            <a href={`${catalog}/tree/main/database`}>Recipe catalog</a>
            <a href={`${repository}/releases`}>Releases</a>
            <a href={repository}>GitHub</a>
          </nav>
        </div>
        <div className={styles.footerLegal}>
          <small>
            © {new Date().getFullYear()} Farin Works. All rights reserved.
          </small>
          <small>
            Licensed under{' '}
            <a href={`${repository}/blob/main/LICENSE`}>GNU GPLv3 or later</a>.
          </small>
        </div>
      </footer>
    </Layout>
  );
}
