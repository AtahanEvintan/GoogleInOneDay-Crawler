/**
 * GoogleInOneDay — Dashboard JavaScript
 *
 * Features:
 * - Tab navigation (Crawler / Status / Search)
 * - Crawl form submission via POST /api/crawl
 * - Live status polling every 1 second
 * - Job history table with actions (pause/resume/stop)
 * - Search with 300ms debounce
 * - Long-poll integration for live search result updates
 * - Toast notifications
 */

const API_BASE = "";

// ── State ───────────────────────────────────────────────────────
let currentTab = "crawler";
let statusPollInterval = null;
let searchDebounceTimer = null;
let longPollController = null;
let lastSearchQuery = "";
let lastIndexVersion = 0;

// ── Init ────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    setupTabs();
    setupCrawlForm();
    setupSearch();
    setupRefreshButton();
    refreshJobsList();
    startGlobalStatusPoll();
});

// ── Tab Navigation ──────────────────────────────────────────────
function setupTabs() {
    document.querySelectorAll(".nav-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            const tabName = tab.dataset.tab;
            switchTab(tabName);
        });
    });
}

function switchTab(tabName) {
    currentTab = tabName;

    // Update tab buttons
    document.querySelectorAll(".nav-tab").forEach((t) => t.classList.remove("active"));
    document.querySelector(`[data-tab="${tabName}"]`).classList.add("active");

    // Update content
    document.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
    document.getElementById(`content-${tabName}`).classList.add("active");

    // Tab-specific actions
    if (tabName === "status") {
        refreshStatus();
    } else if (tabName === "crawler") {
        refreshJobsList();
    }
}

// ── Crawl Form ──────────────────────────────────────────────────
function setupCrawlForm() {
    const form = document.getElementById("crawl-form");
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const btn = document.getElementById("btn-crawl");
        btn.disabled = true;
        btn.textContent = "Starting...";

        const body = {
            origin: document.getElementById("input-origin").value.trim(),
            depth: parseInt(document.getElementById("input-depth").value, 10),
            max_rate: parseFloat(document.getElementById("input-rate").value),
            max_concurrent: parseInt(document.getElementById("input-concurrent").value, 10),
            max_queue: parseInt(document.getElementById("input-queue").value, 10),
        };

        try {
            const resp = await fetch(`${API_BASE}/api/crawl`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const data = await resp.json();

            if (resp.ok) {
                showToast(`Crawl started: Job ${data.job_id}`, "success");
                refreshJobsList();
                // Switch to status tab to show progress
                setTimeout(() => switchTab("status"), 500);
            } else {
                showToast(data.error || "Failed to start crawl", "error");
            }
        } catch (err) {
            showToast(`Network error: ${err.message}`, "error");
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<span class="btn-icon">🕷️</span> Start Crawl';
        }
    });
}

// ── Jobs List (Crawler Tab) ─────────────────────────────────────
function setupRefreshButton() {
    document.getElementById("btn-refresh-jobs").addEventListener("click", refreshJobsList);
}

async function refreshJobsList() {
    try {
        const resp = await fetch(`${API_BASE}/api/jobs`);
        const data = await resp.json();
        renderJobsTable(data.jobs || []);
    } catch (err) {
        console.error("Failed to refresh jobs:", err);
    }
}

function renderJobsTable(jobs) {
    const tbody = document.getElementById("jobs-tbody");

    if (!jobs.length) {
        tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No crawl jobs yet. Start one above!</td></tr>';
        return;
    }

    tbody.innerHTML = jobs
        .map((job) => {
            const statusClass = job.status || "unknown";
            const originShort = truncateUrl(job.origin_url, 40);
            return `
            <tr>
                <td><code style="color: var(--text-secondary); font-size: var(--text-xs)">${job.job_id}</code></td>
                <td class="url-cell" title="${escapeHtml(job.origin_url)}">${escapeHtml(originShort)}</td>
                <td>${job.max_depth}</td>
                <td>
                    <span class="status-badge ${statusClass}">
                        <span class="status-dot"></span>
                        ${statusClass}
                    </span>
                </td>
                <td>${formatNumber(job.pages_crawled)}</td>
                <td>${job.errors || 0}</td>
                <td>
                    <div class="actions-cell">
                        ${renderJobActions(job)}
                    </div>
                </td>
            </tr>`;
        })
        .join("");
}

function renderJobActions(job) {
    if (job.status === "running") {
        return `
            <button class="btn btn-warning btn-sm" onclick="pauseJob('${job.job_id}')">⏸ Pause</button>
            <button class="btn btn-danger btn-sm" onclick="stopJob('${job.job_id}')">⏹ Stop</button>
        `;
    }
    if (job.status === "paused") {
        return `
            <button class="btn btn-success btn-sm" onclick="resumeJob('${job.job_id}')">▶ Resume</button>
            <button class="btn btn-danger btn-sm" onclick="stopJob('${job.job_id}')">⏹ Stop</button>
        `;
    }
    return `<span style="color: var(--text-muted); font-size: var(--text-xs)">—</span>`;
}

// ── Job Actions ─────────────────────────────────────────────────
async function pauseJob(jobId) {
    try {
        await fetch(`${API_BASE}/api/jobs/${jobId}/pause`, { method: "POST" });
        showToast(`Job ${jobId} paused`, "info");
        refreshJobsList();
        refreshStatus();
    } catch (err) {
        showToast("Failed to pause job", "error");
    }
}

async function resumeJob(jobId) {
    try {
        const resp = await fetch(`${API_BASE}/api/jobs/${jobId}/resume`, { method: "POST" });
        const data = await resp.json();
        showToast(`Job resumed as ${data.job_id}`, "success");
        refreshJobsList();
        refreshStatus();
    } catch (err) {
        showToast("Failed to resume job", "error");
    }
}

async function stopJob(jobId) {
    try {
        await fetch(`${API_BASE}/api/jobs/${jobId}/stop`, { method: "POST" });
        showToast(`Job ${jobId} stopped`, "info");
        refreshJobsList();
        refreshStatus();
    } catch (err) {
        showToast("Failed to stop job", "error");
    }
}

// ── Status Polling ──────────────────────────────────────────────
function startGlobalStatusPoll() {
    refreshStatus();
    statusPollInterval = setInterval(refreshStatus, 1000);
}

async function refreshStatus() {
    try {
        const resp = await fetch(`${API_BASE}/api/status`);
        const data = await resp.json();
        updateGlobalMetrics(data);
        updateJobDetails(data.jobs || []);
        updateNavMeta(data);
    } catch (err) {
        // Silently retry on next interval
    }
}

function updateGlobalMetrics(data) {
    animateNumber("metric-active-jobs", data.active_jobs || 0);
    animateNumber("metric-total-pages", data.total_pages || 0);
    animateNumber("metric-total-tokens", data.total_tokens || 0);
    animateNumber("metric-index-version", data.index_version || 0);
}

function updateNavMeta(data) {
    document.getElementById("total-pages-count").textContent = formatNumber(data.total_pages || 0);
    document.getElementById("index-version").textContent = data.index_version || 0;
}

function updateJobDetails(jobs) {
    const container = document.getElementById("job-details-container");
    if (!jobs || jobs.length === 0) {
        container.innerHTML = `<div class="card glass empty-state"><p>No jobs to display. Start a crawl from the Crawler tab.</p></div>`;
        return;
    }

    // Capture scroll state BEFORE destroying the html
    const scrollPositions = {};
    const shouldAutoScroll = {};

    jobs.forEach(job => {
        const term = document.getElementById(`logs-${job.job_id}`);
        if (term) {
            scrollPositions[job.job_id] = term.scrollTop;
            // Auto scroll if scrolled to the very bottom (within 20px)
            shouldAutoScroll[job.job_id] = (term.scrollHeight - term.scrollTop - term.clientHeight) < 20;
        } else {
            shouldAutoScroll[job.job_id] = true; // Default to auto-scroll for new terminals
        }
    });

    container.innerHTML = jobs
        .map((job) => {
            const isRunning = job.status === "running";
            const queuePct = job.max_queue
                ? Math.min(100, ((job.urls_queued || 0) / (job.max_queue || 10000)) * 100)
                : 0;
            const queueColor = queuePct > 80 ? "red" : queuePct > 50 ? "yellow" : "green";

            return `
            <div class="card glass job-detail-card">
                <div class="job-detail-header">
                    <div class="job-detail-title">
                        <span class="status-badge ${job.status}">
                            <span class="status-dot"></span>
                            ${job.status}
                        </span>
                        <h3>${escapeHtml(truncateUrl(job.origin_url, 50))}</h3>
                    </div>
                    <div class="job-detail-actions">
                        ${isRunning ? `
                            <button class="btn btn-warning btn-sm" onclick="pauseJob('${job.job_id}')">⏸ Pause</button>
                            <button class="btn btn-danger btn-sm" onclick="stopJob('${job.job_id}')">⏹ Stop</button>
                        ` : job.status === "paused" ? `
                            <button class="btn btn-success btn-sm" onclick="resumeJob('${job.job_id}')">▶ Resume</button>
                        ` : ""}
                    </div>
                </div>
                <div class="job-stats-grid">
                    <div class="job-stat">
                        <div class="job-stat-value">${formatNumber(job.pages_crawled || 0)}</div>
                        <div class="job-stat-label">Pages Crawled</div>
                    </div>
                    <div class="job-stat">
                        <div class="job-stat-value">${formatNumber(job.urls_discovered || 0)}</div>
                        <div class="job-stat-label">URLs Discovered</div>
                    </div>
                    <div class="job-stat">
                        <div class="job-stat-value">${formatNumber(job.urls_queued || 0)}</div>
                        <div class="job-stat-label">Queue Depth</div>
                        <div class="bp-bar">
                            <div class="bp-bar-fill ${queueColor}" style="width: ${queuePct}%"></div>
                        </div>
                    </div>
                    <div class="job-stat">
                        <div class="job-stat-value">${job.pages_per_second || 0}</div>
                        <div class="job-stat-label">Pages/sec</div>
                    </div>
                    <div class="job-stat">
                        <div class="job-stat-value">${formatDuration(job.elapsed_seconds || 0)}</div>
                        <div class="job-stat-label">Elapsed</div>
                    </div>
                    <div class="job-stat">
                        <div class="job-stat-value">${job.errors || 0}</div>
                        <div class="job-stat-label">Errors</div>
                    </div>
                    ${isRunning ? `
                    <div class="job-stat">
                        <div class="job-stat-value">${job.rate_limit_active ? "⚠️ Active" : "✅ OK"}</div>
                        <div class="job-stat-label">Rate Limit</div>
                    </div>
                    <div class="job-stat">
                        <div class="job-stat-value">${job.queue_full ? "⚠️ Full" : "✅ OK"}</div>
                        <div class="job-stat-label">Backpressure</div>
                    </div>
                    ` : ""}
                </div>
                <div class="job-logs-container">
                    <div class="job-logs-title">Live Log Stream</div>
                    <div class="job-logs-terminal" id="logs-${job.job_id}">
                        ${(job.logs || []).map(log => `<div>${escapeHtml(log)}</div>`).join("")}
                    </div>
                </div>
            </div>`;
        })
        .join("");

    // Restore scroll positions or auto-scroll
    jobs.forEach(job => {
        const term = document.getElementById(`logs-${job.job_id}`);
        if (term) {
            if (shouldAutoScroll[job.job_id] && job.status !== "completed") {
                term.scrollTop = term.scrollHeight;
            } else if (scrollPositions[job.job_id] !== undefined) {
                term.scrollTop = scrollPositions[job.job_id];
            }
        }
    });
}

// ── Search ──────────────────────────────────────────────────────
function setupSearch() {
    const input = document.getElementById("search-input");
    const luckyBtn = document.getElementById("btn-lucky");

    // Debounced search
    input.addEventListener("input", () => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => {
            const q = input.value.trim();
            if (q) {
                performSearch(q);
            } else {
                clearSearchResults();
            }
        }, 300);
    });

    // Feeling Lucky
    luckyBtn.addEventListener("click", async () => {
        try {
            const resp = await fetch(`${API_BASE}/api/random-word`);
            const data = await resp.json();
            if (data.word) {
                input.value = data.word;
                performSearch(data.word);
                showToast(`Lucky word: "${data.word}"`, "info");
            } else {
                showToast("No indexed words yet — start a crawl first!", "info");
            }
        } catch (err) {
            showToast("Failed to get random word", "error");
        }
    });
}

async function performSearch(query) {
    lastSearchQuery = query;

    try {
        const resp = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(query)}&k=50`);
        const data = await resp.json();

        lastIndexVersion = data.index_version || 0;
        renderSearchResults(data);
        startLongPoll(query);
    } catch (err) {
        console.error("Search failed:", err);
        showToast("Search failed", "error");
    }
}

function renderSearchResults(data) {
    const container = document.getElementById("search-results");
    const meta = document.getElementById("search-meta");
    const countEl = document.getElementById("search-result-count");

    meta.style.display = "flex";
    countEl.textContent = `${data.total || 0} result${data.total !== 1 ? "s" : ""}`;

    const results = data.results || [];
    if (!results.length) {
        container.innerHTML = '<div class="card glass empty-state"><p>No results found. Try a different query or wait for more pages to be indexed.</p></div>';
        return;
    }

    // Find max score for relative bar widths
    const maxScore = Math.max(...results.map((r) => r.score || 0), 0.001);

    container.innerHTML = `<div class="card glass" style="padding: 0; overflow: hidden;">
        ${results
            .map(
                (r) => `
            <div class="search-result">
                <div class="result-title">
                    <a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.title || r.url)}</a>
                </div>
                <div class="result-url">${escapeHtml(r.url)}</div>
                <div class="result-meta">
                    <span>📍 Origin: ${escapeHtml(truncateUrl(r.origin_url, 30))}</span>
                    <span>🔗 Depth: ${r.depth}</span>
                    <span>
                        Score:
                        <span class="score-bar">
                            <span class="score-bar-fill" style="width: ${((r.score / maxScore) * 100).toFixed(0)}%"></span>
                        </span>
                        ${r.score.toFixed(3)}
                    </span>
                </div>
            </div>`
            )
            .join("")}
    </div>`;
}

function clearSearchResults() {
    document.getElementById("search-results").innerHTML =
        '<div class="card glass empty-state" id="search-empty"><p>Enter a query to search indexed pages.</p></div>';
    document.getElementById("search-meta").style.display = "none";
    cancelLongPoll();
    lastSearchQuery = "";
}

// ── Long-Poll ───────────────────────────────────────────────────
function startLongPoll(query) {
    cancelLongPoll();
    longPollLoop(query);
}

function cancelLongPoll() {
    if (longPollController) {
        longPollController.abort();
        longPollController = null;
    }
}

async function longPollLoop(query) {
    while (lastSearchQuery === query) {
        longPollController = new AbortController();
        try {
            const resp = await fetch(
                `${API_BASE}/api/updates?q=${encodeURIComponent(query)}&last_version=${lastIndexVersion}&timeout=30`,
                { signal: longPollController.signal }
            );
            const data = await resp.json();

            // Check if user changed query while we were waiting
            if (lastSearchQuery !== query) break;

            if (data.updated) {
                lastIndexVersion = data.index_version;
                renderSearchResults(data);
            }
        } catch (err) {
            if (err.name === "AbortError") break;
            // Network error — retry after delay
            await new Promise((r) => setTimeout(r, 2000));
        }
    }
}

// ── Utility Functions ───────────────────────────────────────────
function formatNumber(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "K";
    return String(n);
}

function formatDuration(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function truncateUrl(url, maxLen) {
    if (!url) return "";
    if (url.length <= maxLen) return url;
    return url.substring(0, maxLen - 3) + "...";
}

function escapeHtml(text) {
    if (!text) return "";
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

function animateNumber(elementId, target) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const current = parseInt(el.textContent.replace(/[^0-9]/g, ""), 10) || 0;
    if (current === target) return;
    el.textContent = formatNumber(target);
    el.style.color = "var(--accent)";
    setTimeout(() => (el.style.color = ""), 500);
}

// ── Toast Notifications ─────────────────────────────────────────
function showToast(message, type = "info") {
    let container = document.querySelector(".toast-container");
    if (!container) {
        container = document.createElement("div");
        container.className = "toast-container";
        document.body.appendChild(container);
    }

    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => toast.remove(), 3000);
}
