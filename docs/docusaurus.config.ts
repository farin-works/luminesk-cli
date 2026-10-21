import { themes as prismThemes } from 'prism-react-renderer';
import type { Config } from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Luminesk-CLI',
  tagline: 'Reproducible Minecraft Java and Bedrock server composer',
  favicon: 'img/favicon.ico',
  headTags: [
  {
    tagName: "meta",
    attributes: {
      property: "og:site_name",
      content: "Luminesk-CLI",
    },
  },
  {
    tagName: "script",
    attributes: {
      type: "application/ld+json",
    },
    innerHTML: JSON.stringify({
      "@context": "https://schema.org",
      "@type": "WebSite",
      name: "Luminesk-CLI",
      url: "https://nesk.farin.cc/",
    }),
  },
  ],



  url: 'https://nesk.farin.cc',
  baseUrl: '/',
  organizationName: 'farin-works',
  projectName: 'luminesk-cli',

  onBrokenLinks: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
        },
        blog: false, // {
        //   showReadingTime: false,
        //   feedOptions: {
        //     type: ['rss', 'atom'],
        //     xslt: true,
        //   },
        //   onInlineTags: 'warn',
        //   onInlineAuthors: 'warn',
        //   onUntruncatedBlogPosts: 'warn',
        // },
        theme: {
          customCss: './src/css/custom.css',
        },
        sitemap: {
          changefreq: 'weekly',
          createSitemapItems: async (params) => {
            const { defaultCreateSitemapItems } = params;
            const items = await defaultCreateSitemapItems(params);
            
            return items.map((item) => {
              if (item.url === 'https://nesk.farin.cc' || item.url === 'https://nesk.farin.cc/') {
                return { ...item, priority: 1.0 };
              }
              
              if (item.url.includes('/docs/getting-started') || item.url.includes('/docs/installation')) {
                return { ...item, priority: 0.8 };
              }
              
              return item;
            });
          },
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/social-card.png',
    colorMode: {
      respectPrefersColorScheme: false,
      defaultMode: 'dark',
      disableSwitch: true,
    },
    navbar: {
      logo: {
        alt: 'Luminesk-CLI',
        src: 'img/logo.svg',
        width: 110,
        height: 22,
      },
      items: [
        { to: '/docs', label: 'Docs', position: 'left' },
        {
          href: 'https://github.com/farin-works/luminesk-cli',
          position: 'right',
          className: 'header-github-link navbar-sidebar__item--hidden',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      style: 'dark',
      // links: [],
      copyright: `© ${new Date().getFullYear()} Farin Works. All rights reserved.<br />
                Luminesk-CLI is licensed under
                <a href="https://github.com/farin-works/luminesk-cli/blob/main/LICENSE">GNU GPLv3 or later</a>.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'powershell', 'json', 'python', 'yaml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
