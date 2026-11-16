const limitedCapabilities = new Set(['list_only', 'partial', 'manual', 'pending']);
export const healthLabels = {ok:'檢索成功', partial:'部分未完成', error:'檢索失敗', pending:'待接通', unverified:'待驗證', stale:'資料已逾時', limited:'已知來源限制', manual:'需手動查看'};
export const capabilityLabels = {full:'清單及詳情', list_only:'只提供清單', partial:'部分詳情已接入', manual:'手動查看', pending:'尚未接入'};

export function sourceStatus(source, staleHours = 36, now = Date.now()) {
  const lastUpdate = source.last_listing_success || source.last_success;
  const stale = source.enabled && lastUpdate && now - Date.parse(lastUpdate) > staleHours * 3600000;
  if (source.severity === 'error' || source.status === 'error') return 'error';
  if (source.capability === 'manual') return 'manual';
  if (!source.enabled || source.capability === 'pending') return 'pending';
  if (stale && (source.status === 'ok' || source.listing_complete)) return 'stale';
  if (limitedCapabilities.has(source.capability) && source.severity === 'warning') return 'limited';
  return source.status || 'unverified';
}

export function sourceRank(source, staleHours = 36, now = Date.now()) {
  return ({error:0, stale:1, partial:2, unverified:3, limited:4, pending:5, manual:6, ok:7})[sourceStatus(source, staleHours, now)] ?? 3;
}

export function detailCoverageLabel(source) {
  if (Number.isInteger(source.detail_complete_count) && Number.isInteger(source.detail_total_count)) {
    return source.detail_total_count ? `${source.detail_complete_count} / ${source.detail_total_count} 則` : source.listing_complete === true ? '本輪清單沒有職位' : '尚未取得可核實詳情';
  }
  return '此版本未提供';
}
