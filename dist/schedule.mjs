export const repository = 'yanyangwogwo-ctrl/teaching-job-radar';
export const workflowURL = `https://raw.githubusercontent.com/${repository}/main/.github/workflows/daily.yml`;
export const editorURL = `https://github.com/${repository}/edit/main/.github/workflows/daily.yml`;

export function parseSchedule(text) {
  const value = String(text);
  const headers = [...value.matchAll(/^  schedule:[ \t]*(?:#[^\n]*)?\r?\n/gm)];
  if (headers.length !== 1) throw new Error('未能辨識目前排程，請直接在 GitHub 檢查。');
  const header = headers[0];
  let body = '';
  for (const line of value.slice(header.index + header[0].length).match(/[^\n]*(?:\n|$)/g) ?? []) {
    const trimmed = line.trim();
    const indent = line.match(/^[ \t]*/)[0].length;
    if (trimmed && !trimmed.startsWith('#') && indent <= 2) break;
    body += line;
  }
  const block = header[0] + body;
  const entries = [...body.matchAll(/^([ \t]+-\s+cron:\s*)(['"])([^'"\n]+)\2[^\n]*$/gm)];
  if (entries.length !== 1) throw new Error('目前有多個或未支援嘅排程，未有改動任何設定。');
  const fields = entries[0][3].match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/);
  const zoneLines = [...body.matchAll(/^[ \t]+timezone:[^\n]*$/gm)];
  if (zoneLines.length > 1) throw new Error('排程包含重複時區，未有改動任何設定。');
  const zone = zoneLines.length ? zoneLines[0][0].match(/^\s+timezone:\s*['"]?([A-Za-z_\/]+)['"]?(?:\s+#.*)?\s*$/)?.[1] : 'UTC';
  if (!fields || !['UTC','Asia/Hong_Kong'].includes(zone) || +fields[1] > 59 || +fields[2] > 23) throw new Error('目前並非每日一次嘅香港／UTC 排程，未有改動任何設定。');
  const hour = (+fields[2] + (zone === 'UTC' ? 8 : 0)) % 24;
  const time = `${String(hour).padStart(2,'0')}:${fields[1].padStart(2,'0')}`;
  return {time, zone, block, cronLine:entries[0][0], prefix:entries[0][1]};
}

export function replaceSchedule(text, time) {
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) throw new Error('請選擇有效嘅香港時間。');
  const current = parseSchedule(text);
  const [hour, minute] = time.split(':').map(Number);
  const cronHour = current.zone === 'UTC' ? (hour + 16) % 24 : hour;
  const line = `${current.prefix}'${minute} ${cronHour} * * *' # ${time} Hong Kong; GitHub may start later.`;
  return text.replace(current.block, current.block.replace(current.cronLine,line));
}

export async function loadSchedule(fetcher = fetch) {
  const controller = new AbortController();
  const timer = setTimeout(()=>controller.abort(),12000);
  try {
    const response = await fetcher(workflowURL, {cache:'no-cache',credentials:'omit',referrerPolicy:'no-referrer',signal:controller.signal});
    if (!response.ok) throw new Error('未能讀取 GitHub 最新排程，請稍後再試。');
    const text = await response.text();
    return {...parseSchedule(text),text};
  } finally { clearTimeout(timer); }
}
