import React, { type ReactNode } from 'react';
import Link from '@docusaurus/Link';
import { ArrowRight, ArrowUpRight } from '@phosphor-icons/react';
import styles from './Landing.module.css';

export default function Header(): ReactNode {
  return (
    <header className={styles.header}>
      <nav className={styles.navigation} aria-label="Main navigation">
        <Link to="/" className={styles.brand} aria-label="Luminesk-CLI home">
          <img src="/img/logo.svg" alt="Luminesk-CLI" width="130" height="26" />
        </Link>
        <div className={styles.desktopLinks}>
          <a href="/#how-it-works">How it works</a>
          <a href="/#recipes">Recipes</a>
          <a href="/#security">Security</a>
        </div>
        <div className={styles.navActions}>
          <Link to="/docs">Docs</Link>
          <a
            href="https://github.com/farin-works/luminesk-cli"
            className={styles.githubLink}
          >
            GitHub <ArrowUpRight aria-hidden="true" />
          </a>
          <a href="/#installation" className={styles.navInstall}>
            Install <ArrowRight aria-hidden="true" />
          </a>
        </div>
      </nav>
    </header>
  );
}
