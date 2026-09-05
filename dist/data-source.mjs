export function validDataset(value) {
  if (value?.meta?.schema_version !== 1 || !Array.isArray(value.jobs) || !Array.isArray(value.sources)) throw new Error('資料格式不符');
  return value;
}

async function readJSON(fetcher, url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetcher(url, { cache:'no-cache', credentials:'omit', referrerPolicy:'no-referrer', signal:controller.signal });
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
      const dataset = validDataset(await readJSON(fetcher, url.href, timeoutMs));
      return { dataset, mode:'cloud', warning:'' };
    }
  } catch {
    warning = '未能取得雲端最新資料，目前顯示網站保存嘅舊版本。請留意下方檢索日期，稍後重新載入。';
  }
  const dataset = validDataset(await readJSON(fetcher, './data/jobs.json', timeoutMs));
  return { dataset, mode:'snapshot', warning };
}

export function lastCrawlAt(dataset) {
  return dataset.meta.last_run?.finished_at || dataset.meta.last_run?.started_at || '';
}
