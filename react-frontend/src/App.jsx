import { useEffect, useMemo, useState } from "react";

const REFRESH_SECONDS = 15;

function statusClass(value) {
  const s = String(value || "").toLowerCase();
  if (s.includes("running") || s === "active" || s === "online" || s === "up") return "ok";
  if (s.includes("inactive") || s.includes("exited") || s.includes("failed") || s.includes("not_found")) return "bad";
  return "warn";
}

function probeStatusClass(probe) {
  if (!probe?.enabled) return "warn";
  if (probe?.is_stale) return "bad";
  if (probe?.latest_run?.ok === true) return "ok";
  if (probe?.latest_run?.ok === false) return "bad";
  return "warn";
}

function formatIso(value) {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

function summarizeProbeSteps(payload) {
  const steps = payload?.steps;
  if (!Array.isArray(steps) || steps.length === 0) return "-";
  const failed = steps.filter((s) => s && !s.skipped && !s.ok).length;
  return `${steps.length - failed}/${steps.length} checks passed`;
}

async function apiFetch(path, token, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-RC-Token": token || "",
    "X-RC-Actor": "rc-ui",
    ...(options.headers || {})
  };
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) {
    const message = payload.error || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function metric(value, suffix = "") {
  if (value === null || value === undefined) return "-";
  return `${value}${suffix}`;
}

function formatPercent(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return `${Number(n).toFixed(2)}%`;
}

function bytesToGb(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return `${(Number(n) / (1024 ** 3)).toFixed(2)} GB`;
}

function bytesToHuman(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = Number(n);
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

function diskCardClass(item) {
  if (!item) return "warn";
  if (item.is_alert) return "bad";
  const used = Number(item.used_pct ?? 0);
  const inodeUsed = Number(item.inodes_used_pct ?? 0);
  if (used >= 75 || inodeUsed >= 75) return "warn";
  return "ok";
}

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem("rc_token") || "");
  const [status, setStatus] = useState(null);
  const [auditRows, setAuditRows] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [probeBusy, setProbeBusy] = useState({});

  const services = status?.targets?.services || [];
  const containers = status?.targets?.containers || [];
  const tcpChecks = status?.targets?.tcp_checks || [];
  const scheduledProbes = status?.scheduled_probes || [];
  const diskReport = status?.disk_report || {};
  const diskFilesystems = diskReport?.filesystems || [];
  const diskWatchPaths = diskReport?.watch_paths || [];
  const diskAlerts = diskReport?.alerts || [];
  const rootFs = useMemo(
    () => diskFilesystems.find((fs) => fs.mount === "/") || null,
    [diskFilesystems]
  );

  const firstService = services[0]?.name || "";
  const firstContainer = containers[0]?.name || "";

  const [targetType, setTargetType] = useState("service");
  const [action, setAction] = useState("restart");
  const [target, setTarget] = useState(firstService);

  useEffect(() => {
    if (targetType === "service") {
      setTarget(firstService || "");
    } else {
      setTarget(firstContainer || "");
    }
  }, [targetType, firstService, firstContainer]);

  const targetOptions = useMemo(() => {
    if (targetType === "service") {
      return services.map((s) => s.name);
    }
    return containers.map((c) => c.name);
  }, [targetType, services, containers]);

  const degradedProbeCount = useMemo(
    () => scheduledProbes.filter((p) => p?.enabled && (p?.is_stale || p?.latest_run?.ok === false)).length,
    [scheduledProbes]
  );

  async function refresh() {
    if (!token) {
      setError("Set API token first.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const [statusPayload, auditPayload] = await Promise.all([
        apiFetch("/api/v1/status", token),
        apiFetch("/api/v1/audit?limit=25", token)
      ]);
      setStatus(statusPayload.data);
      setAuditRows(auditPayload.rows || []);
    } catch (ex) {
      setError(ex.message || String(ex));
    } finally {
      setBusy(false);
    }
  }

  async function executeAction(ev) {
    ev.preventDefault();
    if (!token) {
      setError("Set API token first.");
      return;
    }
    if (!target) {
      setError("Select a target.");
      return;
    }
    setActionBusy(true);
    setError("");
    try {
      await apiFetch("/api/v1/action", token, {
        method: "POST",
        body: JSON.stringify({
          target_type: targetType,
          action,
          target,
          reason
        })
      });
      await refresh();
      setReason("");
    } catch (ex) {
      setError(ex.message || String(ex));
    } finally {
      setActionBusy(false);
    }
  }

  async function runProbeNow(key) {
    if (!token) {
      setError("Set API token first.");
      return;
    }
    setProbeBusy((prev) => ({ ...prev, [key]: true }));
    setError("");
    try {
      await apiFetch("/api/v1/probes/run", token, {
        method: "POST",
        body: JSON.stringify({ key })
      });
      await refresh();
    } catch (ex) {
      setError(ex.message || String(ex));
    } finally {
      setProbeBusy((prev) => ({ ...prev, [key]: false }));
    }
  }

  useEffect(() => {
    if (!token) return;
    refresh();
    const id = setInterval(refresh, REFRESH_SECONDS * 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  function onTokenChange(v) {
    setToken(v);
    sessionStorage.setItem("rc_token", v);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>DIRE Remote Control</h1>
          <p>Metal-hosted control board for deployment operations.</p>
        </div>
        <button onClick={refresh} disabled={busy || !token}>
          {busy ? "Refreshing..." : "Refresh"}
        </button>
      </header>

      <section className="panel">
        <h2>Authentication</h2>
        <div className="auth-row">
          <input
            type="password"
            value={token}
            onChange={(e) => onTokenChange(e.target.value)}
            placeholder="RC API token"
          />
          <span className="hint">Stored in this browser session only.</span>
        </div>
      </section>

      {error ? <div className="error-banner">{error}</div> : null}

      <section className="grid-metrics">
        <div className="metric-card">
          <h3>Host</h3>
          <p>{metric(status?.host)}</p>
        </div>
        <div className="metric-card">
          <h3>Timestamp (UTC)</h3>
          <p>{metric(status?.timestamp_utc)}</p>
        </div>
        <div className="metric-card">
          <h3>Uptime</h3>
          <p>{metric(status?.uptime_seconds, " sec")}</p>
        </div>
        <div className="metric-card">
          <h3>Memory</h3>
          <p>{formatPercent(status?.memory?.used_pct)}</p>
        </div>
        <div className="metric-card">
          <h3>Disk /</h3>
          <p>{formatPercent(rootFs?.used_pct ?? status?.disk_root?.used_pct)}</p>
        </div>
        <div className="metric-card">
          <h3>Disk / Used</h3>
          <p>{bytesToGb(rootFs?.used_bytes ?? status?.disk_root?.used_bytes)}</p>
        </div>
        <div className="metric-card">
          <h3>Disk / Inodes</h3>
          <p>{formatPercent(rootFs?.inodes_used_pct)}</p>
        </div>
        <div className="metric-card">
          <h3>Expensive Probes</h3>
          <p>
            {scheduledProbes.length - degradedProbeCount}/{scheduledProbes.length} healthy
          </p>
        </div>
        <div className="metric-card">
          <h3>Disk Alerts</h3>
          <p>{diskAlerts.length}</p>
        </div>
      </section>

      <section className="panel">
        <h2>Administrative Action</h2>
        <form className="action-form" onSubmit={executeAction}>
          <select value={targetType} onChange={(e) => setTargetType(e.target.value)}>
            <option value="service">Service</option>
            <option value="container">Container</option>
          </select>
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            {targetOptions.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="restart">Restart</option>
            <option value="start">Start</option>
            <option value="stop">Stop</option>
          </select>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason for audit log (optional)"
          />
          <button type="submit" disabled={actionBusy || !token}>
            {actionBusy ? "Executing..." : "Execute"}
          </button>
        </form>
      </section>

      <section className="panel">
        <h2>Scheduled Probes</h2>
        <div className="grid-list">
          {scheduledProbes.length === 0 ? (
            <p className="hint">No scheduled probes configured.</p>
          ) : (
            scheduledProbes.map((probe) => (
              <article key={probe.key} className={`status-card ${probeStatusClass(probe)}`}>
                <div className="probe-head">
                  <h3>{probe.key}</h3>
                  <button
                    type="button"
                    className="mini-btn"
                    onClick={() => runProbeNow(probe.key)}
                    disabled={!token || probeBusy[probe.key]}
                  >
                    {probeBusy[probe.key] ? "Running..." : "Run now"}
                  </button>
                </div>
                <p>Type: {probe.type || "-"}</p>
                <p>Status: {probe.latest_run?.status || "never-run"}</p>
                <p>Latest OK: {probe.latest_run?.ok === null || probe.latest_run?.ok === undefined ? "-" : String(probe.latest_run.ok)}</p>
                <p>Age: {probe.age_seconds === null || probe.age_seconds === undefined ? "-" : `${probe.age_seconds} sec`}</p>
                <p>Stale: {String(Boolean(probe.is_stale))}</p>
                <p>Last run: {formatIso(probe.last_run_at)}</p>
                <p>Next run: {formatIso(probe.next_run_at)}</p>
                <p>Checks: {summarizeProbeSteps(probe.latest_run?.payload)}</p>
                {probe.latest_run?.error ? <p className="error-text">Error: {probe.latest_run.error}</p> : null}
              </article>
            ))
          )}
        </div>
      </section>

      <section className="panel">
        <h2>Disk Filesystems</h2>
        {diskReport?.errors?.length ? (
          <div className="error-banner">
            {diskReport.errors.map((e, i) => (
              <div key={i}>{e}</div>
            ))}
          </div>
        ) : null}
        <p className="hint">
          Snapshot: {formatIso(diskReport?.collected_at)} | Alerts when space or inode use is above
          {` ${diskReport?.alert_used_pct ?? 85}%`}
        </p>
        <div className="grid-list">
          {diskFilesystems.length === 0 ? (
            <p className="hint">No filesystem data available.</p>
          ) : (
            diskFilesystems.map((fs) => (
              <article key={`${fs.filesystem}-${fs.mount}`} className={`status-card ${diskCardClass(fs)}`}>
                <h3>{fs.mount}</h3>
                <p>Device: {fs.filesystem}</p>
                <p>Type: {fs.fs_type}</p>
                <p>Used: {formatPercent(fs.used_pct)} ({bytesToHuman(fs.used_bytes)} / {bytesToHuman(fs.total_bytes)})</p>
                <p>Free: {bytesToHuman(fs.free_bytes)}</p>
                <p>Inodes: {formatPercent(fs.inodes_used_pct)}</p>
              </article>
            ))
          )}
        </div>
      </section>

      <section className="panel">
        <h2>Disk Path Usage</h2>
        <div className="grid-list">
          {diskWatchPaths.length === 0 ? (
            <p className="hint">No watched paths configured.</p>
          ) : (
            diskWatchPaths.map((row) => (
              <article key={row.path} className={`status-card ${row.is_alert ? "bad" : "ok"}`}>
                <h3>{row.path}</h3>
                <p>Path Size: {bytesToHuman(row.du_bytes)}</p>
                <p>Mount: {row.mount || "-"}</p>
                <p>FS Used: {formatPercent(row.fs_used_pct)}</p>
                <p>Inodes: {formatPercent(row.fs_inodes_used_pct)}</p>
                {row.error ? <p className="error-text">{row.error}</p> : null}
              </article>
            ))
          )}
        </div>
      </section>

      <section className="panel">
        <h2>Services</h2>
        <div className="grid-list">
          {services.map((s) => (
            <article key={s.name} className={`status-card ${statusClass(s.status)}`}>
              <h3>{s.name}</h3>
              <p>Status: {s.status || "-"}</p>
              <p>Sub: {s.sub_status || "-"}</p>
              <p>Enabled: {s.enabled || "-"}</p>
              {s.error ? <p className="error-text">{s.error}</p> : null}
            </article>
          ))}
        </div>
      </section>

      <section className="panel">
        <h2>Containers</h2>
        <div className="grid-list">
          {containers.map((c) => (
            <article key={c.name} className={`status-card ${statusClass(c.status)}`}>
              <h3>{c.name}</h3>
              <p>Status: {c.status || "-"}</p>
              <p>Image: {c.image || "-"}</p>
              <p>Ports: {c.ports || "-"}</p>
              {c.error ? <p className="error-text">{c.error}</p> : null}
            </article>
          ))}
        </div>
      </section>

      <section className="panel">
        <h2>TCP Checks</h2>
        <div className="grid-list">
          {tcpChecks.map((c) => (
            <article key={`${c.name}-${c.host}-${c.port}`} className={`status-card ${c.ok ? "ok" : "bad"}`}>
              <h3>{c.name}</h3>
              <p>
                {c.host}:{c.port}
              </p>
              <p>Latency: {metric(c.latency_ms, " ms")}</p>
              {!c.ok ? <p className="error-text">{c.error || "Connection failed"}</p> : null}
            </article>
          ))}
        </div>
      </section>

      <section className="panel">
        <h2>Audit Trail</h2>
        <div className="audit-list">
          {auditRows.length === 0 ? (
            <p className="hint">No audit entries yet.</p>
          ) : (
            auditRows
              .slice()
              .reverse()
              .map((row, idx) => (
                <div key={idx} className="audit-row">
                  <span>{row.timestamp_utc || "-"}</span>
                  <span>{row.actor || "-"}</span>
                  <span>{row.target_type || "-"}</span>
                  <span>{row.target || "-"}</span>
                  <span>{row.action || "-"}</span>
                  <span className={row.ok ? "ok-text" : "error-text"}>{String(row.ok)}</span>
                  <span>{row.reason || "-"}</span>
                </div>
              ))
          )}
        </div>
      </section>
    </div>
  );
}
