export function validDataset(value) {
  if (value?.meta?.schema_version !== 1 || !Array.isArray(value.jobs) || !Array.isArray(value.sources)) throw new Error('資料格式不符');
  return value;
}

async function readJSON(fetcher, url, timeoutMs, credentials = 'same-origin') {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    // Local assets need the viewer's Site session. External feeds opt out.
    const response = await fetcher(url, { cache:'no-cache', credentials, referrerPolicy:'no-referrer', signal:controller.signal });
    if (!response.ok) throw new Error('資料未能載入');
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function loadDataset(fetcher = fetch, timeoutMs = 12000) {
  let warning = '';
  try {
    const feed = await readJSON(fetcher, './feed-config.json', timeoutMs);
    if (feed.enabled) {
      const url = new URL(feed.url);
      if (url.protocol !== 'https:' || url.hostname !== 'raw.githubusercontent.com' || url.username || url.password) throw new Error('資料來源設定未能確認');
      const dataset = validDataset(await readJSON(fetcher, url.href, timeoutMs, 'omit'));
      return { dataset, mode:'cloud', warning:'', dataURL:url.href };
    }
  } catch {
    warning = '未能取得雲端最新資料，目前顯示網站保存嘅舊版本。請留意下方檢索日期，稍後重新載入。';
  }
  const dataset = validDataset(await readJSON(fetcher, './data/jobs.json', timeoutMs));
  return { dataset, mode:'snapshot', warning, dataURL:'./data/jobs.json' };
}

export function needsArchive(dataset, criteria) {
  return dataset.meta.feed_scope === 'recent' && String(criteria.recentDays) === 'all';
}

export async function loadArchive(dataset, dataURL, fetcher = fetch, timeoutMs = 12000) {
  if (dataset.meta.feed_scope !== 'recent') return dataset;
  const descriptor = dataset.meta.archive;
  // Archives are generated next to the public feed. Never let feed content
  // redirect a visitor to an unrelated host, repository, or credentialed URL.
  if (descriptor?.url !== 'jobs-archive.json') throw new Error('歷史資料位置未能確認');
  const base = new URL(dataURL, 'https://radar.invalid/');
  const external = base.hostname !== 'radar.invalid';
  if (base.protocol !== 'https:' || base.username || base.password ||
      (external && base.hostname !== 'raw.githubusercontent.com') ||
      (!external && dataURL !== './data/jobs.json')) throw new Error('歷史資料來源未能確認');
  const url = external ? new URL(descriptor.url, base).href : './data/jobs-archive.json';
  const archive = validDataset(await readJSON(fetcher, url, timeoutMs, external ? 'omit' : 'same-origin'));
  const ids = new Set(archive.jobs.map(job => job.id));
  if (archive.meta.generated_at !== dataset.meta.generated_at ||
      descriptor.generation_id !== dataset.meta.generated_at || archive.meta.feed_scope !== 'full' ||
      archive.jobs.length !== descriptor.count || ids.size !== archive.jobs.length ||
      dataset.jobs.some(job => !ids.has(job.id))) {
    throw new Error('歷史資料版本未能對上，請重新載入網站');
  }
  return archive;
}

export function lastCrawlAt(dataset) {
  return dataset.meta.last_run?.finished_at || dataset.meta.last_run?.started_at || '';
}
