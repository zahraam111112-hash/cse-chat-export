const $ = (s, r = document) => r.querySelector(s);
const form = $('#form');
const urlsBox = $('#urls');
const go = $('#go');
const statusBox = $('#status');
const result = $('#result');

function say(text, isErr = false) {
  statusBox.hidden = false;
  statusBox.textContent = text;
  statusBox.classList.toggle('err', isErr);
}

async function poll(jobId, onTick) {
  for (let i = 0; i < 240; i++) {
    const r = await fetch(`api/jobs/${jobId}`);
    if (!r.ok) throw new Error('job not found');
    const job = await r.json();
    onTick(job);
    if (job.status === 'done' || job.status === 'failed') return job;
    await new Promise((res) => setTimeout(res, 1500));
  }
  throw new Error('timeout');
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// A 422 from the API carries FastAPI's validation list, not a string, so the
// browser used to show "Error: [object Object]" for a link it rejected.
function detailOf(data, fallback) {
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail) && data.detail.length) {
    return data.detail.map((d) => (d && d.msg) || JSON.stringify(d)).join('; ');
  }
  return fallback;
}

// A dashboard title comes from someone else's conversation, so it goes into
// the HTML only after being escaped as an attribute value *and* as text.
function titleCell(j) {
  const titles = j.titles || (j.title ? [j.title] : []);
  if (titles.length) {
    const more = j.conversations > 1 ? `+${j.conversations - 1}` : '';
    const rest = titles.slice(1).join(' · ');
    const tip = [titles[0], rest].filter(Boolean).join(' · ');
    return `<span class="tname" title="${esc(tip)}">${esc(titles[0])}</span>` +
      (more ? `<span class="tcount">${more}</span>` : '');
  }
  if (j.status === 'queued' || j.status === 'running') return '<span class="muted">Extracting…</span>';
  return '<span class="muted">—</span>';
}

function statusBadge(j) {
  // A batch where some links failed still holds the ones that worked; calling
  // that plain "done" hides a real failure from whoever reads the table.
  const partial = j.status === 'done' && j.error;
  const label = partial ? 'partial' : j.status;
  const tip = partial ? `${j.error}` : '';
  return `<span class="badge ${label}"${tip ? ` title="${esc(tip)}"` : ''}>${label}</span>`;
}

let shown = 20;

const jobsQuery = () => {
  const q = $('#q').value.trim();
  const status = $('#f-status').value;
  const source = $('#f-source').value;
  return new URLSearchParams({ limit: shown, q, status, source });
};

async function refreshJobs(limit = shown) {
  shown = limit;
  const tbody = $('#jobs');
  const params = jobsQuery();
  const r = await fetch(`api/jobs?${params}`);
  const jobs = await r.json();
  const total = Number(r.headers.get('X-Total') ?? jobs.length);
  tbody.innerHTML = jobs.length
    ? jobs.map((j) => `
      <tr data-job="${j.id}">
        <td class="mono">${j.id}</td>
        <td>${statusBadge(j)}</td>
        <td class="title">${titleCell(j)}</td>
        <td>${esc(j.source)}</td>
        <td class="url">${esc(j.urls[0])}</td>
        <td class="mono t" data-ts="${j.created_at}">${new Date(j.created_at * 1000).toLocaleString('en-GB')}</td>
        <td class="acts">
          <button class="mini" data-open="${j.id}">View</button>
          ${j.status === 'done' ? `
            <a class="mini lk" href="/api/jobs/${j.id}/pdf">PDF</a>
            <a class="mini lk" href="/api/jobs/${j.id}/docx">Word</a>
            <a class="mini lk" href="/api/jobs/${j.id}/md">MD</a>
            <a class="mini lk" href="/api/jobs/${j.id}/json">JSON</a>` : ''}
          ${j.status === 'failed' || (j.status === 'done' && j.error) ? `
            <button class="mini retry" data-retry="${j.id}" title="Run this job again">↻</button>` : ''}
          <button class="mini del" data-del="${j.id}"
                  data-name="${esc(j.title || j.id)}" title="Delete this job and its data">×</button>
        </td>
      </tr>`).join('')
    : `<tr><td colspan="7" class="empty">${filtered() ? 'No job matches this search.' : 'No jobs yet.'}</td></tr>`;
  const more = $('#more');
  if (more) more.hidden = jobs.length >= total;
  $('#count').textContent = filtered()
    ? `${total} matching`
    : total > jobs.length ? `${jobs.length} of ${total}` : '';
  $('#clear').hidden = !filtered();
  // Kept in the URL so a reload, or a link pasted to someone else, shows the
  // same set of jobs.
  const url = filtered() ? `?${params}` : location.pathname;
  history.replaceState(null, '', url);
}

const filtered = () => Boolean($('#q').value.trim() || $('#f-status').value || $('#f-source').value);

let searchTimer;
function searchChanged() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { shown = 20; refreshJobs(20); }, 200);
}

function showJob(job) {
  result.innerHTML = '';
  const tpl = $('#viewer').content.cloneNode(true);
  const wrap = document.createElement('div');
  wrap.appendChild(tpl);
  const viewer = $('.viewer', wrap);
  const items = job.transcripts || [];
  $('.vtitle', viewer).textContent =
    items.map((t) => t.title).join(' + ') || `job ${job.id}`;
  const chars = items.reduce((a, t) => a + t.chars, 0);
  const msgs = items.reduce((a, t) => a + t.messages.length, 0);
  $('.vmeta', viewer).textContent =
    `${items.length} conversation(s) · ${msgs} messages · ${chars.toLocaleString('en-GB')} chars · ${job.id}`;
  $('.dl-pdf', viewer).href = `api/jobs/${job.id}/pdf`;
  $('.dl-docx', viewer).href = `api/jobs/${job.id}/docx`;
  $('.dl-md', viewer).href = `api/jobs/${job.id}/md`;
  $('.dl-json', viewer).href = `api/jobs/${job.id}/json`;
  $('.close', viewer).onclick = () => (result.innerHTML = '');

  const body = $('.vbody', viewer);
  // The serverside renderer turns `$…$` into MathML, so the preview shows the
  // same formulas as the PDF instead of raw LaTeX. Until each message arrives
  // its text is still shown, escaped, so the viewer never opens blank.
  items.forEach((t, ti) => {
    t.messages.forEach((m, mi) => {
      const div = document.createElement('div');
      div.className = `msg ${m.role === 'user' ? 'user' : 'assistant'}`;
      div.innerHTML =
        `<div class="who">${m.role === 'user' ? '👤 User' : '🤖 ChatGPT'}</div>` +
        `<div class="content">${esc(m.content)}</div>`;
      body.appendChild(div);
      // Only a message that has something to convert is worth a round trip;
      // a plain one is already what the server would send back.
      if (!/[\\$]/.test(m.content)) return;
      const box = $('.content', div);
      fetch(`api/jobs/${job.id}/html/${ti}/${mi}`)
        .then((r) => (r.ok ? r.text() : null))
        .then((html) => {
          if (html === null) return;
          box.innerHTML = html;
          box.classList.add('rich');
        })
        .catch(() => {});  // the escaped text stays, which is not an error
    });
  });
  result.appendChild(viewer);
}

// --- per-link progress ------------------------------------------------------
// A batch is several jobs, one per link, so each link reports the moment it is
// done instead of the whole batch appearing at the end.

const prog = $('#progress');
const pfill = $('#pfill');
const plabel = $('#plabel');
const pcount = $('#pcount');
const plist = $('#plist');

function startProgress(targets) {
  prog.hidden = false;
  plist.innerHTML = targets.map((t) => `
    <li data-p="${t.id}">
      <span class="pico wait">○</span>
      <span class="plink" title="${esc(t.url)}">${esc(t.url)}</span>
      <span class="ptime">queued</span>
    </li>`).join('');
  paintProgress(0, targets.length);
}

function paintProgress(done, total) {
  pfill.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
  plabel.textContent = done >= total ? 'All links finished' : 'Working through your links…';
  pcount.textContent = `${done} of ${total} done`;
}

function markLink(target, cls, icon, note) {
  const li = plist.querySelector(`li[data-p="${target.id}"]`);
  if (!li) return;
  const ico = li.querySelector('.pico');
  ico.className = `pico ${cls}`;
  ico.textContent = icon;
  li.querySelector('.ptime').textContent = note;
}

async function followLink(target, state) {
  // There are fewer workers than links, so a link spends time waiting for one.
  // Showing every link as "running" would hide that; follow the row's status.
  markLink(target, 'wait', '○', 'queued');
  try {
    const job = await poll(target.id, (j) => {
      if (j.status === 'running') markLink(target, 'work', '●', 'running');
    });
    if (job.status === 'failed') {
      markLink(target, 'bad', '✕', 'failed');
    } else {
      const chars = (job.transcripts || []).reduce((a, t) => a + (t.chars || 0), 0);
      markLink(target, 'ok', '✓', `${chars.toLocaleString('en-GB')} chars`);
    }
    return job;
  } catch (err) {
    markLink(target, 'bad', '✕', 'error');
    throw err;
  } finally {
    state.done += 1;
    paintProgress(state.done, state.total);
  }
}

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const urls = urlsBox.value.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  if (!urls.length) return say('Enter at least one link.', true);
  if (urls.length > 5) return say('Up to 5 links per request.', true);

  go.disabled = true;
  try {
    say('Submitting…');
    let targets;
    let note = '';
    if (urls.length === 1) {
      const r = await fetch('api/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: urls[0] }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailOf(data, 'request failed'));
      targets = [{ id: data.job_id, url: urls[0] }];
    } else {
      const r = await fetch('api/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(detailOf(data, 'request failed'));
      // One job per accepted link, in order. Use the server's `accepted` list:
      // `urls` is what we sent, so a rejected link in front would pair every
      // later job with the wrong address in the progress rows.
      const accepted = data.accepted || urls;
      targets = data.job_ids.map((id, i) => ({ id, url: accepted[i] }));
      if (data.rejected && data.rejected.length) {
        note = ` · skipped ${data.rejected.length}: ` +
          data.rejected.map((x) => `${x.url} (${x.reason})`).join('; ');
      }
    }

    startProgress(targets);
    say(`Queued ${targets.length} job${targets.length > 1 ? 's' : ''}${note}`);

    const state = { done: 0, total: targets.length };
    const settled = await Promise.allSettled(targets.map((t) => followLink(t, state)));
    const finished = settled.filter((s) => s.status === 'fulfilled').map((s) => s.value);
    const failed = finished.filter((j) => j.status === 'failed');
    const ok = finished.filter((j) => j.status !== 'failed');

    if (ok.length) {
      // Only a batch where every link failed is an error; a partial batch is
      // still a success with a note, or the user reads it as a total failure.
      const missing = failed.length ? ` · ${failed.length} failed, see the list above` : '';
      say(`Ready · ${ok.length} of ${targets.length} extracted${missing}${note}`);
    } else {
      const why = failed.map((j) => j.error).filter(Boolean).join('; ');
      say(`Failed: ${why || 'no details'}${note}`, true);
    }
    // With one link the viewer opens on it; with several, each has its own row
    // in the table and its own Word/MD/JSON buttons.
    if (ok.length === 1 && targets.length === 1) showJob(ok[0]);
    await refreshJobs();
    await refreshStats();
  } catch (err) {
    say(`Error: ${err.message}`, true);
  } finally {
    go.disabled = false;
  }
});

async function refreshStats() {
  const box = $('#kpi-jobs');
  if (!box) return;  // public visitors get no global counters
  const r = await fetch('api/stats');
  if (!r.ok) return;
  const s = await r.json();
  box.textContent = s.jobs;
  $('#kpi-done').textContent = s.done;
  $('#kpi-partial').textContent = s.partial;
  $('#kpi-failed').textContent = s.failed;
  $('#kpi-users').textContent = s.users;
  $('#kpi-chars').textContent = s.chars.toLocaleString('en-GB');
}

$('#q').addEventListener('input', searchChanged);
$('#f-status').addEventListener('change', searchChanged);
$('#f-source').addEventListener('change', searchChanged);
$('#clear').addEventListener('click', () => {
  $('#q').value = '';
  $('#f-status').value = '';
  $('#f-source').value = '';
  shown = 20;
  refreshJobs(20);
});

// The server renders the first page without any filter, so restore whatever
// the URL asks for before the user sees an unfiltered table.
(() => {
  const p = new URLSearchParams(location.search);
  $('#q').value = p.get('q') || '';
  const status = p.get('status') || '';
  const source = p.get('source') || '';
  if ([...$('#f-status').options].some((o) => o.value === status)) $('#f-status').value = status;
  if ([...$('#f-source').options].some((o) => o.value === source)) $('#f-source').value = source;
  if (filtered()) refreshJobs(20);
})();

document.addEventListener('click', async (ev) => {
  if (ev.target.id === 'more') {
    ev.target.disabled = true;
    await refreshJobs(shown + 50);
    ev.target.disabled = false;
    return;
  }

  const retryId = ev.target.dataset && ev.target.dataset.retry;
  if (retryId) {
    ev.target.disabled = true;
    say(`Retrying ${retryId}…`);
    try {
      const r = await fetch(`api/jobs/${retryId}/retry`, { method: 'POST' });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `Retry failed (${r.status})`);
      const job = await poll(retryId, (j) => say(`Job ${j.id} · status: ${j.status}`));
      if (job.status === 'failed') say(`Failed: ${job.error || 'no details'}`, true);
      else {
        say(`Ready · ${job.transcripts ? job.transcripts.length : 0} conversation(s)`);
        showJob(job);
      }
    } catch (err) {
      say(`Error: ${err.message}`, true);
    } finally {
      await refreshJobs();
      await refreshStats();
    }
    return;
  }

  const delId = ev.target.dataset && ev.target.dataset.del;
  if (delId) {
    const name = ev.target.dataset.name || delId;
    if (!confirm(`Delete "${name}" and its extracted data? This cannot be undone.`)) return;
    const r = await fetch(`api/jobs/${delId}`, { method: 'DELETE' });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      return say(data.detail || `Delete failed (${r.status})`, true);
    }
    say(`Deleted ${delId}`);
    // The viewer may be showing the job that was just removed.
    const shownJob = result.querySelector('.vmeta')?.textContent || '';
    if (shownJob.includes(delId)) result.innerHTML = '';
    await refreshJobs();
    await refreshStats();
    return;
  }

  const id = ev.target.dataset && ev.target.dataset.open;
  if (!id) return;
  say(`Loading ${id}…`);
  const r = await fetch(`api/jobs/${id}`);
  const job = await r.json();
  if (job.transcripts) {
    say(`Showing ${id}`);
    showJob(job);
    result.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } else {
    say(`This job has no output yet (${job.status})`, true);
  }
});

document.querySelectorAll('#jobs .t').forEach((cell) => {
  const d = new Date(parseFloat(cell.dataset.ts || cell.textContent) * 1000);
  cell.textContent = d.toLocaleString('en-GB');
});

// The count that hides older entries is not obvious from a table that just
// stops, so offer the next batch whenever there is one.
if (document.querySelectorAll('#jobs tr[data-job]').length >= 20) $('#more').hidden = false;
