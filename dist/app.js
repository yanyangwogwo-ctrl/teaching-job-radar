import { mountScheduleEditor } from './schedule-ui.mjs?v=20260905-time';
import { parseQuery, filterJobs, dayKey, effectiveStatus, csvCell, defaultCriteria, restoreCriteria, recentCutoff, isRecent } from './search.mjs?v=20260905-compact';
import { loadDataset, lastCrawlAt } from './data-source.mjs?v=20260905-session';

const app = document.querySelector('#app');
const exportButton = document.querySelector('#export-button');
const detailDialog = document.querySelector('#job-dialog');
const saveDialog = document.querySelector('#save-dialog');
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeURL = value => { try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : '#'; } catch { return '#'; } };
const employmentLabels = { 'part-time':'兼職', hourly:'時薪', mixed:'全職／兼職', 'full-time':'全職', unknown:'聘用類型未明' };
const statusLabels = { open:'開放', closed:'已截止', missing:'已消失' };
const healthLabels = { ok:'檢索成功', partial:'部分未完成', error:'檢索失敗', pending:'待接通', unverified:'待驗證', stale:'資料已逾時' };
let dataset, criteria, visibleJobs = [], view = 'jobs', toastTimer;
let saved = [];
let detailOpener = null;
let feedWarning = '', feedMode = 'snapshot';

function initialCriteria() { return defaultCriteria(dataset); }
function toast(message) { const node = document.querySelector('#toast'); node.textContent = message; node.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; }, 3500); }
function dateTime(value) { if (!value) return '尚未成功檢索'; const date = new Date(value); return Number.isNaN(+date) ? '日期不明' : new Intl.DateTimeFormat('zh-HK', { timeZone:'Asia/Hong_Kong', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false }).format(date); }
function isStale(source) { return source.enabled && source.last_success && Date.now() - +new Date(source.last_success) > dataset.meta.stale_after_hours * 3600000; }
function sourceStatus(source) { return source.status === 'ok' && isStale(source) ? 'stale' : source.status; }
function healthBadge(source) { const status = sourceStatus(source); return `<span class="status-badge status-${escapeHTML(status)}">${healthLabels[status] ?? '狀態不明'}</span>`; }

function loadSaved() {
  try {
    const value = JSON.parse(localStorage.getItem('teaching-radar-searches-v1') || '[]');
    saved = Array.isArray(value) ? value.filter(v => v && typeof v.name === 'string' && v.criteria && Array.isArray(v.criteria.institutions)).slice(0, 20) : [];
  } catch { saved = []; }
}
function storeSaved(next) {
  try { localStorage.setItem('teaching-radar-searches-v1', JSON.stringify(next)); saved = next; return true; }
  catch { toast('瀏覽器未能儲存搜尋，請檢查儲存空間或私隱模式。'); return false; }
}

function renderShell() {
  const connected = dataset.sources.filter(s => sourceStatus(s) === 'ok').length;
  const matched = filterJobs(dataset.jobs, initialCriteria()).length;
  const active = dataset.jobs.filter(j => isRecent(j) && effectiveStatus(j) === 'open').length;
  document.querySelector('.schedule-label').textContent = feedMode === 'snapshot' ? '網站保存版本 · 請留意檢索日期' : dataset.meta.automation_active ? dataset.meta.schedule : '已連接雲端資料 · 等待首次自動檢索';
  app.innerHTML = `
    <section class="page-heading"><h1>職位一覽</h1><p class="updated-at">最近檢索 ${escapeHTML(dateTime(lastCrawlAt(dataset)))} <span>· 香港時間</span></p></section>
    <div class="view-bar">
      <nav class="view-nav" aria-label="檢索頁面"><button type="button" class="view-button active" data-view="jobs" aria-pressed="true">搜尋職位</button><button type="button" class="view-button" data-view="sources" aria-pressed="false">來源狀態 <span>${dataset.sources.length}</span></button><button type="button" class="view-button" data-view="notifications" aria-pressed="false">通知與排程</button><button type="button" class="view-button" data-view="presets" aria-pressed="false">預設條件</button></nav>
      <div class="compact-stats" aria-label="資料摘要"><span>搜尋結果 <strong id="matched-total">${matched}</strong></span><span title="最近兩個月開放職位；刊登日期優先，缺少時用首見">兩個月內開放 <strong>${active}</strong></span><span title="最近檢索完整成功，且資料未逾時的來源">成功來源 <strong>${connected}<small>/${dataset.sources.length}</small></strong></span></div>
    </div>
    <section id="jobs-view" class="workspace">
      <aside class="filter-panel" aria-label="職位篩選"><details class="filter-drawer" ${matchMedia('(min-width: 960px)').matches ? 'open' : ''}><summary>院校及日期篩選 <span>展開／收起</span></summary><div class="filter-content">
        <div class="filter-heading"><h2>篩選條件</h2><button type="button" class="text-button" id="reset-filters">重設</button></div>
        <fieldset><legend>院校</legend><div class="selection-actions"><button type="button" class="text-button" id="select-all">全選</button><button type="button" class="text-button" id="select-none">全部取消</button></div><div class="institution-list">${dataset.sources.map(source => `<label class="institution-option"><input type="checkbox" name="institution" value="${escapeHTML(source.id)}" checked><span>${escapeHTML(source.institution)}${!source.enabled ? '<small>待接通</small>' : ''}</span><span class="count">${dataset.jobs.filter(j => j.source_id === source.id && isRecent(j)).length}</span></label>`).join('')}</div></fieldset>
        <fieldset><legend>日期範圍</legend><label class="field-label" for="date-basis">日期依據</label><select id="date-basis"><option value="effective">刊登日期（缺少時用首見）</option><option value="first_seen">系統首見日期</option><option value="deadline">截止日期</option></select><div class="date-range"><label>由<input type="date" id="date-from"></label><label>至<input type="date" id="date-to"></label></div><p class="field-help">以上日期可進一步收窄結果。兩個月限制始終按刊登／首見日期計算，唔會用截止日期代替。</p></fieldset>
        <label class="field-label" for="job-status">職位狀態</label><select id="job-status"><option value="open">開放職位</option><option value="all">全部狀態（最近兩個月）</option><option value="closed">已截止</option><option value="missing">已消失</option></select>
      </div></details></aside>
      <div class="results-panel" id="results" tabindex="-1">
        <section class="search-panel" aria-label="搜尋條件">
          <div class="search-heading"><label for="query-input">關鍵字</label><div class="mode-switch" role="group" aria-label="關鍵字匹配方式"><button type="button" data-mode="AND" aria-pressed="true" class="selected">全部 AND</button><button type="button" data-mode="OR" aria-pressed="false">任一 OR</button></div></div><input type="search" id="query-input" placeholder='例如 philosophy, lecturer 或 "AI literacy"' maxlength="500" autocomplete="off" aria-describedby="syntax-help"><p id="syntax-help" class="syntax-help">搜尋職位名、部門、科目及內文。逗號／空格分隔；<code>"AI literacy"</code> 完整詞組；<code>-nursing</code> 排除。</p><label class="exclude-label" for="exclude-input">排除關鍵字<input type="text" id="exclude-input" placeholder="例如 nursing, accounting" maxlength="500"></label>
          <div class="search-bottom"><button type="button" class="preset-toggle" id="preset-toggle" role="switch" aria-checked="true" aria-label="套用預設條件"><span class="switch-track" aria-hidden="true"></span><span id="preset-toggle-label">預設條件：開啟</span></button><button type="button" class="text-button" id="save-search">＋ 儲存搜尋</button></div><p class="query-feedback" id="query-feedback" role="status"></p><div id="saved-searches" class="saved-searches"></div>
        </section>
        ${feedWarning ? `<div class="notice results-notice" role="alert"><strong>${escapeHTML(feedWarning)}</strong></div>` : ''}
        <div id="coverage-warning" class="notice results-notice" role="status"></div>
        <p class="recency-note" id="recency-note"></p>
        <div class="result-toolbar"><p id="result-count" aria-live="polite"></p><label for="sort-by">排序<select id="sort-by"><option value="newest">日期由新到舊</option><option value="oldest">日期由舊到新</option><option value="score">相關度優先</option><option value="deadline">即將截止優先</option><option value="institution">院校名稱</option></select></label></div>
        <div id="job-list" class="job-list"></div>
        <p class="results-footnote">相關度分數沿用預設科目，唔會限制自訂關鍵字搜尋。請到官方原文核對學歷、經驗及最新申請情況。</p>
      </div>
    </section>
    <section id="sources-view" class="secondary-view" hidden></section>
    <section id="notifications-view" class="secondary-view" hidden></section>
    <section id="presets-view" class="secondary-view" hidden>
      <div class="section-title"><h2>預設搜尋條件</h2><p>開啟「預設條件」時，會連同搜尋頁嘅關鍵字、院校及日期一齊篩選。修改即時套用；可返回搜尋頁儲存常用搜尋。</p></div>
      <section class="preset-panel search-panel" aria-label="編輯預設條件">
        <div class="criteria-heading"><h3>科目及職位關鍵字</h3><button type="button" class="text-button" id="reset-preset">還原原始預設</button></div>
        <div class="search-heading"><label for="preset-input">合併關鍵字</label><div class="mode-switch" role="group" aria-label="預設關鍵字匹配方式"><button type="button" data-preset-mode="AND" aria-pressed="false">全部 AND</button><button type="button" data-preset-mode="OR" aria-pressed="true" class="selected">任一 OR</button></div></div>
        <textarea id="preset-input" rows="7" maxlength="5000" aria-describedby="preset-help">${escapeHTML(criteria.presetKeywords)}</textarea>
        <p id="preset-help" class="field-help">逗號或換行分隔，詞組內嘅空格保留。所有關鍵字都搜尋職位名、部門、科目及內文。清空即可取消關鍵字限制。</p>
        <p class="field-help">科目與職位名稱已合併。選「任一 OR」時，符合其中一個詞便可，例如只有 lecturer 亦會符合；選「全部 AND」則每個詞都要符合。</p>
        <label class="checkbox-label employment-filter"><input type="checkbox" id="part-time-only" ${criteria.partTimeOnly ? 'checked' : ''}>只看兼職／時薪（包括同時招聘全職及兼職）</label>
        <div class="preset-footer"><p class="field-help" id="preset-state"></p><button type="button" class="button" id="back-to-results">返回搜尋結果</button></div>
        <p class="field-help">呢度只修改網頁搜尋，唔會改變 Discord 正式通知條件。</p>
      </section>
    </section>
    <footer class="site-footer">保留歷史記錄 · 檢索失敗唔會清空舊職位 · 未提供嘅日期唔會估填</footer>`;
  app.setAttribute('aria-busy', 'false');
  const problems = dataset.sources.filter(s => s.enabled && sourceStatus(s) !== 'ok');
  const pending = dataset.sources.filter(s => !s.enabled).length;
  const warning = document.querySelector('#coverage-warning');
  const lastCrawl = lastCrawlAt(dataset);
  const crawlOverdue = !lastCrawl || Date.now() - +new Date(lastCrawl) > dataset.meta.stale_after_hours * 3600000;
  if (crawlOverdue) warning.innerHTML = '<strong>已超過 36 小時未有新一輪檢索。</strong> 目前顯示歷史資料，請檢查每日排程；這不代表沒有新職位。';
  else if (problems.length) warning.innerHTML = `<strong>${problems.length} 個來源未能提供完整、有效期內嘅資料。</strong> 舊記錄仍然保留；查看「來源狀態」了解詳情。`;
  else if (pending) warning.innerHTML = `<strong>目前已接通 ${connected} 個來源，另外 ${pending} 個待接通。</strong> 目前結果只涵蓋已接通來源。`;
  else { warning.classList.add('notice-success'); warning.textContent = '所有來源最近一次檢索均已完成。'; }
  bindControls();
  syncControls();
  renderSaved();
  renderResults();
}

function bindControls() {
  matchMedia('(min-width: 960px)').addEventListener('change', event => { if (event.matches) document.querySelector('.filter-drawer').open = true; });
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => changeView(button.dataset.view)));
  document.querySelectorAll('[name="institution"]').forEach(input => input.addEventListener('change', () => { criteria.institutions = [...document.querySelectorAll('[name="institution"]:checked')].map(i => i.value); renderResults(); }));
  document.querySelector('#select-all').addEventListener('click', () => { criteria.institutions = dataset.sources.map(s => s.id); syncControls(); renderResults(); });
  document.querySelector('#select-none').addEventListener('click', () => { criteria.institutions = []; syncControls(); renderResults(); });
  document.querySelector('#reset-filters').addEventListener('click', resetFilters);
  document.querySelector('#reset-preset').addEventListener('click', () => { const defaults = initialCriteria(); for (const key of ['presetKeywords','presetMode','partTimeOnly']) criteria[key] = defaults[key]; syncControls(); renderResults(); });
  document.querySelector('#preset-toggle').addEventListener('click', () => { criteria.presetEnabled = !criteria.presetEnabled; syncControls(); renderResults(); });
  document.querySelector('#back-to-results').addEventListener('click', () => { changeView('jobs'); document.querySelector('#query-input').focus(); });
  for (const [id, key] of [['preset-input','presetKeywords'],['query-input','query'],['exclude-input','exclude'],['date-from','from'],['date-to','to'],['date-basis','dateBasis'],['job-status','status'],['sort-by','sort']]) {
    document.querySelector('#' + id).addEventListener(id.includes('input') ? 'input' : 'change', event => { criteria[key] = event.target.value; renderResults(); });
  }
  document.querySelector('#part-time-only').addEventListener('change', event => { criteria.partTimeOnly = event.target.checked; renderResults(); });
  document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => { criteria.mode = button.dataset.mode; syncControls(); renderResults(); }));
  document.querySelectorAll('[data-preset-mode]').forEach(button => button.addEventListener('click', () => { criteria.presetMode = button.dataset.presetMode; syncControls(); renderResults(); }));
  document.querySelector('#save-search').addEventListener('click', openSaveDialog);
  document.querySelector('#job-list').addEventListener('click', event => { const button = event.target.closest('[data-job]'); if (button) openJob(button.dataset.job, button); if (event.target.closest('[data-reset]')) resetFilters(); });
}

function syncControls() {
  for (const [id, key] of [['preset-input','presetKeywords'],['query-input','query'],['exclude-input','exclude'],['date-from','from'],['date-to','to'],['date-basis','dateBasis'],['job-status','status'],['sort-by','sort']]) document.querySelector('#' + id).value = criteria[key];
  document.querySelector('#part-time-only').checked = criteria.partTimeOnly;
  document.querySelector('#preset-toggle').setAttribute('aria-checked', String(criteria.presetEnabled));
  document.querySelector('#preset-toggle-label').textContent = '預設條件：' + (criteria.presetEnabled ? '開啟' : '關閉');
  document.querySelector('#preset-state').textContent = criteria.presetEnabled ? '目前已開啟預設條件。' : '目前已關閉預設條件；返回搜尋頁開啟後才會套用。';
  document.querySelectorAll('[name="institution"]').forEach(input => { input.checked = criteria.institutions.includes(input.value); });
  document.querySelectorAll('[data-mode]').forEach(button => { const active = button.dataset.mode === criteria.mode; button.classList.toggle('selected', active); button.setAttribute('aria-pressed', String(active)); });
  document.querySelectorAll('[data-preset-mode]').forEach(button => { const active = button.dataset.presetMode === criteria.presetMode; button.classList.toggle('selected', active); button.setAttribute('aria-pressed', String(active)); });
}
function resetFilters() { criteria = initialCriteria(); syncControls(); renderResults(); }

function deadlineLabel(job) {
  if (job.deadline_type === 'closing' && job.deadline) return escapeHTML(job.deadline);
  if (job.deadline_type === 'until-filled') return '持續招聘';
  if (job.deadline_type === 'review') return (job.deadline ? escapeHTML(job.deadline) + '<small>開始審閱，非截止</small>' : '審閱中，截止未明');
  if (job.deadline_type === 'screening-or-closing') return '日期性質待核實' + (job.deadline_raw ? '<small>' + escapeHTML(job.deadline_raw) + '</small>' : '');
  return '網站未提供';
}

function renderResults() {
  const feedback = document.querySelector('#query-feedback');
  const invalidRange = criteria.from && criteria.to && criteria.from > criteria.to;
  feedback.textContent = invalidRange ? '開始日期不可遲過結束日期。' : parseQuery(criteria.query).unclosedQuote ? '提示：詞組嘅雙引號未關閉。' : '';
  feedback.hidden = !feedback.textContent;
  visibleJobs = invalidRange ? [] : filterJobs(dataset.jobs, criteria);
  document.querySelector('#matched-total').textContent = visibleJobs.length;
  document.querySelector('#recency-note').textContent = `最近兩個月：${recentCutoff()} 至 ${dayKey(new Date().toISOString())} · 刊登日期優先，缺少時用首見。`;
  document.querySelector('#result-count').innerHTML = `<strong>${visibleJobs.length}</strong> 則符合條件 <span>／ 共 ${dataset.jobs.length} 則記錄</span>`;
  exportButton.disabled = view !== 'jobs' || visibleJobs.length === 0;
  if (!visibleJobs.length) {
    const reason = !criteria.institutions.length ? '你未選擇任何院校。' : '最近兩個月未有符合以上條件嘅職位。可以修改關鍵字，或關閉預設條件。';
    document.querySelector('#job-list').innerHTML = `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">⌕</span><h2>未有符合條件嘅職位</h2><p>${reason}</p><button type="button" class="button button-outline" data-reset>重設篩選</button><p class="field-help">結果只涵蓋已接通來源；其他來源或故障詳見「來源狀態」。</p></div>`;
    return;
  }
  document.querySelector('#job-list').innerHTML = visibleJobs.map(job => {
    const status = effectiveStatus(job);
    const source = dataset.sources.find(s => s.id === job.source_id);
    const stale = source && sourceStatus(source) !== 'ok';
    const evidence = job.evidence?.[0];
    return `<article class="job-card ${job.matches ? 'matched' : ''}"><div class="job-card-top"><div class="job-meta"><span class="institution-badge">${escapeHTML(job.institution)}</span><span>${escapeHTML(employmentLabels[job.employment_type] || job.employment_type)}</span>${status !== 'open' ? `<span class="status-badge status-${status}">${statusLabels[status]}</span>` : ''}${stale || !job.detail_complete ? '<span class="stale-label">資料待核實</span>' : ''}</div><a class="button button-outline source-link" href="${escapeHTML(safeURL(job.url))}" target="_blank" rel="noopener noreferrer">官方原文 ↗<span class="sr-only">：${escapeHTML(job.title)}</span></a></div><div class="job-main"><h2><button type="button" class="job-title" data-job="${escapeHTML(job.id)}">${escapeHTML(job.title)}</button></h2><p class="job-department">${escapeHTML(job.department || '網站未列明部門')}</p><div class="subject-tags">${(job.subjects || []).map(s => `<span>${escapeHTML(s.label)}</span>`).join('')}${job.matches ? `<span class="match-score" title="規則匹配分數，並非獲聘機率">相關度 ${job.score}</span>` : ''}</div>${evidence ? `<p class="match-evidence">匹配內容：${escapeHTML(evidence.snippet)}</p>` : ''}</div><div class="job-dates"><div><span>${job.posted_date ? '刊登日期' : '首見日期'}</span><strong>${escapeHTML(job.posted_date || dayKey(job.first_seen))}</strong>${!job.posted_date ? '<small>網站未提供刊登日期</small>' : ''}</div><div><span>截止日期</span><strong>${deadlineLabel(job)}</strong></div></div></article>`;
  }).join('');
}

function changeView(next) {
  view = next;
  for (const name of ['jobs','sources','notifications','presets']) document.querySelector('#' + name + '-view').hidden = name !== view;
  document.querySelectorAll('[data-view]').forEach(button => { const active = button.dataset.view === view; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
  exportButton.disabled = view !== 'jobs' || visibleJobs.length === 0;
  if (view === 'sources') renderSources();
  if (view === 'notifications') renderNotifications();
}

function sourceExplanation(source) {
  if (!source.enabled) return '尚未接入每日檢索，目前需要手動查看。原因見下方備註；系統唔會每日重試已停用來源。';
  if (sourceStatus(source) === 'ok') return '最近一次已讀完整個職位清單及可用詳情。';
  const error = (source.errors ?? []).join(' ');
  if (/ConnectTimeout|ReadTimeout|逾時|timeout/i.test(error)) return '上次連線逾時，唔代表網站永久封鎖。已保留舊記錄，下一輪每日檢索會再次嘗試。';
  if (['hkust','cityu'].includes(source.id)) return '清單已接通。首次檢查發現詳情頁有存取限制，目前每日只更新清單；內文仍需到官方網站查看。';
  if (/404/.test(error)) return '清單已讀取，但部分職位原文或附件連結失效，令詳情未能完整收集。';
  if (source.id === 'cuhk' && source.status === 'partial') return '已取得職位及詳情，但網站回報嘅總數同實際分頁結果不一致，可能仍有遺漏，所以標示部分完成。';
  if (/202|驗證|challenge/i.test(error)) return '清單或部分詳情可讀，但網站要求額外驗證。已停止受影響嘅讀取，並保留取得嘅資料。';
  return '最近一次未能完整更新；已保留舊記錄，具體原因見下方。';
}

function renderSources() {
  document.querySelector('#sources-view').innerHTML = `<div class="section-title"><h2>來源狀態及未完成原因</h2><p>「部分未完成」仍然可能已讀到清單；「檢索失敗」係今輪未讀到；「待接通」則尚未啟用。人手開到網站，唔代表程式亦能讀到全部職位。</p></div><div class="source-grid">${dataset.sources.map(source => `<article class="source-card"><div class="source-card-top"><h3>${escapeHTML(source.institution)}</h3><a class="button button-outline" href="${escapeHTML(safeURL(source.url))}" target="_blank" rel="noopener noreferrer">官方招聘 ↗<span class="sr-only">：${escapeHTML(source.institution)}</span></a></div><div class="source-health">${healthBadge(source)}</div><p>${escapeHTML(sourceExplanation(source))}</p><dl><div><dt>最近成功</dt><dd>${escapeHTML(dateTime(source.last_success))}</dd></div><div><dt>最近嘗試</dt><dd>${source.last_attempt ? escapeHTML(dateTime(source.last_attempt)) : '尚未執行'}</dd></div><div><dt>最近取得</dt><dd>${source.last_attempt ? source.count + ' 則' : '—'}</dd></div></dl>${source.errors?.length ? `<p class="source-error">${source.errors.map(escapeHTML).join('<br>')}</p>` : ''}<p class="source-note">${escapeHTML(source.notes)}</p></article>`).join('')}</div>`;
}

function renderNotifications() {
  const meta = dataset.meta;
  const rule = meta.notification_rule;
  document.querySelector('#notifications-view').innerHTML = `<div class="section-title"><h2>通知與每日檢索</h2><p>此處顯示正式通知條件。網頁上儲存嘅搜尋唔會自動改變 Discord 通知。</p></div><div id="schedule-editor"></div><div class="setup-grid"><article class="setup-card"><span class="step-number">01</span><h3>每日自動檢索</h3><span class="status-badge ${meta.automation_active ? 'status-ok' : 'status-pending'}">${meta.automation_active ? '已由雲端排程執行' : '尚未啟用'}</span><p>按上方 GitHub 已儲存時間，每日檢索一次。</p><p class="field-help">${meta.automation_active ? '此狀態反映資料產生時的執行環境；來源超過 36 小時未成功會標示逾時。' : '網站已設定讀取 GitHub 最新資料；首次雲端檢索完成後會顯示執行時間。每日工作流程會持續更新同一個資料來源。'}</p></article><article class="setup-card"><span class="step-number">02</span><h3>Discord 職位通知</h3><span class="status-badge ${meta.discord_configured ? 'status-ok' : 'status-pending'}">${meta.discord_configured ? '已設定' : '尚未設定'}</span><p>首次匯入建立基準；之後通知新發現而且符合條件嘅職位。</p><p class="field-help">${meta.discord_configured ? '最近確認接收：' + escapeHTML(dateTime(meta.notifications?.last_success)) : '需要你指定 Discord 頻道。Webhook 密鑰應儲存在雲端 Secrets，毋須貼在此網頁。'}</p>${meta.notifications?.error ? `<p class="source-error">${escapeHTML(meta.notifications.error)}</p>` : ''}</article><article class="setup-card"><span class="step-number">03</span><h3>漏跑警告</h3><span class="status-badge ${meta.heartbeat_configured ? 'status-ok' : 'status-pending'}">${meta.heartbeat_configured ? '已設定回報' : '獨立監察尚未設定'}</span><p>爬取失敗會留下警告；排程完全冇啟動，需要另一個服務監察。</p><p class="field-help">網頁會自行標示超過 36 小時未更新；主動發出漏跑通知則需要啟用獨立監察。</p></article></div><article class="notification-rules"><h3>正式通知條件</h3><dl><div><dt>職位</dt><dd>兼職／時薪 Lecturer、Tutor、Instructor（包括兼職 Teacher 別名）</dd></div><div><dt>科目</dt><dd>${meta.subjects.map(s => escapeHTML(s.label)).join('、')}</dd></div><div><dt>院校</dt><dd>${rule.institutions.length ? rule.institutions.map(escapeHTML).join('、') : '所有已接通來源'}</dd></div><div><dt>排除關鍵字</dt><dd>${rule.exclude.length ? rule.exclude.map(escapeHTML).join('、') : '未設定'}</dd></div><div><dt>最低相關度</dt><dd>${rule.min_score} / 100（規則分數，並非獲聘機率）</dd></div></dl><p class="field-help">純技術 AI、一般市場營銷 AI 不會單憑「AI」兩字進入通知。符合科目亦仍需核對個別應徵要求。</p></article>`;
  mountScheduleEditor(document.querySelector('#schedule-editor'), time => { document.querySelector('.schedule-label').textContent = '每日約 ' + time + ' · 香港時間'; });
}

function openJob(id, opener) {
  const job = dataset.jobs.find(j => j.id === id);
  if (!job) return;
  detailOpener = opener;
  detailDialog.innerHTML = `<div class="dialog-heading"><span class="institution-badge">${escapeHTML(job.institution)}</span><div class="dialog-top-actions"><a class="button button-outline" href="${escapeHTML(safeURL(job.url))}" target="_blank" rel="noopener noreferrer">官方原文 ↗</a><button type="button" class="icon-button" data-close aria-label="關閉職位詳情">✕</button></div></div><h2 id="job-dialog-title">${escapeHTML(job.title)}</h2><div class="detail-meta"><span>${escapeHTML(employmentLabels[job.employment_type])}</span><span>${escapeHTML(job.reference || '未列明編號')}</span><span>${escapeHTML(statusLabels[effectiveStatus(job)])}</span></div><dl class="detail-dates"><div><dt>刊登日期</dt><dd>${escapeHTML(job.posted_date || '網站未提供')}</dd></div><div><dt>系統首見</dt><dd>${escapeHTML(dayKey(job.first_seen))}</dd></div><div><dt>截止／審閱</dt><dd>${deadlineLabel(job)}</dd></div><div><dt>最近見到</dt><dd>${escapeHTML(dateTime(job.last_seen))}</dd></div></dl>${!job.detail_complete ? '<p class="notice">最近一次未能讀完整份詳情。下方可能顯示上次保存嘅內容，請查看官方原文。</p>' : ''}${job.evidence?.length ? `<section class="detail-evidence"><h3>點解同你相關？</h3>${job.evidence.map(e => `<p><strong>${escapeHTML(e.subject)}</strong> · ${escapeHTML(e.snippet)}</p>`).join('')}</section>` : ''}<h3>職位內文</h3><div class="job-description">${escapeHTML(job.description || '尚未成功取得詳情，請到官方招聘頁查看。')}</div><div class="dialog-footer"><button type="button" class="button button-outline" data-close>關閉</button></div>`;
  detailDialog.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => detailDialog.close()));
  detailDialog.showModal();
}
detailDialog.addEventListener('close', () => detailOpener?.focus());

function renderSaved() {
  const container = document.querySelector('#saved-searches');
  container.innerHTML = saved.length ? `<span class="saved-label">常用搜尋<span>只儲存在此瀏覽器</span></span>${saved.map((entry, index) => `<span class="saved-chip"><button type="button" data-load="${index}">${escapeHTML(entry.name)}</button><button type="button" data-delete="${index}" aria-label="移除搜尋：${escapeHTML(entry.name)}">×</button></span>`).join('')}` : '';
  container.hidden = !saved.length;
  container.querySelectorAll('[data-load]').forEach(button => button.addEventListener('click', () => { const entry = saved[Number(button.dataset.load)]; criteria = restoreCriteria(entry.criteria, dataset); syncControls(); renderResults(); toast('已套用：' + entry.name); }));
  container.querySelectorAll('[data-delete]').forEach(button => button.addEventListener('click', () => { const next = saved.filter((_, i) => i !== Number(button.dataset.delete)); if (storeSaved(next)) { renderSaved(); toast('已移除常用搜尋。'); } }));
}

function openSaveDialog() {
  if (saved.length >= 20) { toast('最多儲存 20 組搜尋，請先移除不再使用嘅組合。'); return; }
  saveDialog.innerHTML = `<form method="dialog" id="save-form"><h2 id="save-dialog-title">儲存常用搜尋</h2><label class="field-label" for="search-name">搜尋名稱</label><input id="search-name" name="name" maxlength="40" required placeholder="例如 哲學兼職教學" autofocus><p class="field-help">包括關鍵字、院校、日期及排序。只儲存在此瀏覽器，唔會改變正式通知條件。</p><div class="dialog-footer"><button type="button" class="button button-outline" id="cancel-save">取消</button><button type="submit" class="button">儲存</button></div></form>`;
  document.querySelector('#cancel-save').addEventListener('click', () => saveDialog.close());
  document.querySelector('#save-form').addEventListener('submit', event => { event.preventDefault(); const name = document.querySelector('#search-name').value.trim(); if (!name) return; if (storeSaved([...saved, { name, criteria:structuredClone(criteria) }])) { saveDialog.close(); renderSaved(); toast('已儲存搜尋：' + name); } });
  saveDialog.showModal();
}

function exportCSV() {
  if (!visibleJobs.length || view !== 'jobs') return;
  const headers = ['院校','職位','部門','聘用類型','刊登日期','首見日期','截止日期','日期性質','職位編號','科目','相關度','狀態','官方連結','內文'];
  const rows = visibleJobs.map(job => [job.institution, job.title, job.department, employmentLabels[job.employment_type], job.posted_date, dayKey(job.first_seen), job.deadline, {closing:'截止',review:'開始審閱', 'until-filled':'持續招聘','screening-or-closing':'審閱／截止未能確認',unknown:'未提供'}[job.deadline_type], job.reference, job.subjects.map(s => s.label).join('、'), job.score, statusLabels[effectiveStatus(job)], job.url, job.description]);
  const blob = new Blob(['\uFEFF' + [headers,...rows].map(row => row.map(csvCell).join(',')).join('\r\n')], {type:'text/csv;charset=utf-8'});
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `teaching-jobs-${dayKey(new Date().toISOString())}.csv`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast(`已匯出 ${visibleJobs.length} 則篩選結果，可用 Excel 開啟。`);
}
exportButton.addEventListener('click', exportCSV);
exportButton.disabled = true;

try {
  const loaded = await loadDataset();
  dataset = loaded.dataset;
  feedWarning = loaded.warning;
  feedMode = loaded.mode;
  criteria = initialCriteria(); loadSaved(); renderShell();
} catch {
  app.setAttribute('aria-busy', 'false');
  app.innerHTML = '<section class="empty-state fatal-state" role="alert"><h1>暫時未能載入職位資料</h1><p>這不代表沒有職位。請稍後重新載入，或查看各院校官方招聘頁。</p><button type="button" class="button" id="retry-load">重新載入</button></section>';
  document.querySelector('#retry-load').addEventListener('click', () => location.reload());
}
