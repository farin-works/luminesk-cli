import React, { type ReactNode, useEffect, useState } from 'react';
import Link from '@docusaurus/Link';
import { ArrowUpRight, Copy } from '@phosphor-icons/react';
import styles from './Landing.module.css';

function Command({
  command,
  label,
}: {
  command: string;
  label: string;
}): ReactNode {
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState('');
  useEffect(() => {
    setReady(true);
  }, []);
  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(command);
      setStatus('Copied.');
    } catch {
      setStatus('Copy unavailable. Select and copy the command manually.');
    }
  }
  return (
    <div className={styles.commandBlock}>
      <div className={styles.command}>
        <pre tabIndex={0} aria-label={label}>
          <code>{command}</code>
        </pre>
        {ready && (
          <button type="button" onClick={copy} aria-label={`Copy ${label}`}>
            <Copy size={19} aria-hidden="true" />
          </button>
        )}
      </div>
      <span className={styles.copyStatus} role="status">
        {status}
      </span>
    </div>
  );
}

export default function Installation(): ReactNode {
  return (
    <section
      id="installation"
      className={`${styles.container} ${styles.installation}`}
      aria-labelledby="installation-title"
    >
      <div className={styles.installIntro}>
        <h2 id="installation-title">Install Luminesk</h2>
        <p>
          Install <code>nesk</code>, choose a recipe, review the plan. Then
          bring your world online.
        </p>
        <p className={styles.requirements}>
          Docker Engine or Docker Desktop is required for servers. Python 3.13+
          is required only for Python tool installs. Windows uses Linux
          containers.
        </p>
        <Link to="/docs/installation" className={styles.textLink}>
          Full installation guide <ArrowUpRight aria-hidden="true" />
        </Link>
      </div>
      <div className={styles.installMethods}>
        <details open>
          <summary>
            One-line installer <span>No Python required</span>
          </summary>
          <div className={styles.installBody}>
            <h3>Linux / macOS</h3>
            <Command
              command="curl -fsSL https://nesk.farin.cc/sh | sh -s -- --yes"
              label="Linux or macOS installer"
            />
            <h3>Windows PowerShell</h3>
            <Command
              command="irm https://nesk.farin.cc/ps1 | iex"
              label="Windows installer"
            />
            <p>
              The installer verifies the binary’s SHA-256 before installing.
              Review the <a href="https://nesk.farin.cc/sh">Unix</a> or{' '}
              <a href="https://nesk.farin.cc/ps1">PowerShell</a> script
              before running it. Installer approval does not accept the
              Minecraft EULA.
            </p>
          </div>
        </details>
        <details>
          <summary>
            Install with uv <span>Python 3.13+</span>
          </summary>
          <div className={styles.installBody}>
            <p>With uv and Python 3.13+ installed:</p>
            <Command
              command="uv tool install luminesk-cli"
              label="uv installation command"
            />
            <p>Keep using the same installation method for upgrades.</p>
          </div>
        </details>
        <details>
          <summary>
            Prebuilt bundles <span>Choose OS + architecture</span>
          </summary>
          <div className={styles.installBody}>
            <p>
              Includes Python. Choose the target machine, verify{' '}
              <a href="https://github.com/farin-works/luminesk-cli/releases/latest/download/SHA256SUMS">
                SHA256SUMS
              </a>
              , then extract the complete ZIP.
            </p>
            <div className={styles.bundles}>
              {[
                ['linux', 'Linux'],
                ['macos', 'macOS'],
                ['windows', 'Windows'],
              ].map(([os, label]) => (
                <div key={os}>
                  <h3>{label}</h3>
                  {['amd64', 'arm64'].map((arch) => (
                    <a
                      key={arch}
                      href={`https://github.com/farin-works/luminesk-cli/releases/latest/download/luminesk-${os}-${arch}.zip`}
                    >
                      {label} {arch.toUpperCase()}{' '}
                      <ArrowUpRight aria-hidden="true" />
                    </a>
                  ))}
                </div>
              ))}
            </div>
          </div>
        </details>
        <div className={styles.verify}>
          <span>Check your environment</span>
          <code>
            nesk --version
            <br />
            nesk doctor
          </code>
          <Link to="/docs/quick-start">
            Continue to your first server <ArrowUpRight aria-hidden="true" />
          </Link>
        </div>
      </div>
    </section>
  );
}
