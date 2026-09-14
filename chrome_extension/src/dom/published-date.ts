const chinaOffsetMs = 8 * 60 * 60 * 1000;
const dayMs = 24 * 60 * 60 * 1000;

/** Page dates use China time. Midnight is a date-only transport convention, not an observed posting time. */
export function publishedTimestamp(raw: string, now: Date = new Date()): string | undefined {
  const text = raw.trim().replace(/^(?:发布于|编辑于|更新于)\s*/u, "");
  const localNow = new Date(now.getTime() + chinaOffsetMs);
  const day = (date: Date): string => `${date.toISOString().slice(0, 10)}T00:00:00+08:00`;
  if (text === "刚刚") return day(localNow);
  const ago = /^(\d{1,4})\s*(秒|分钟|小时|天)前(?:\s+\S+)?$/u.exec(text);
  if (ago !== null) {
    const unit = { 秒: 1000, 分钟: 60_000, 小时: 3_600_000, 天: dayMs }[ago[2]!];
    if (unit !== undefined) return day(new Date(localNow.getTime() - Number(ago[1]) * unit));
  }
  const relative = /^(今天|昨天|前天)(?:\s+(\d{1,2}):(\d{2}))?(?:\s+[^\d\s]\S*)?$/u.exec(text);
  if (relative !== null) {
    if (Number(relative[2] ?? 0) > 23 || Number(relative[3] ?? 0) > 59) return undefined;
    return day(new Date(localNow.getTime() - ({ 今天: 0, 昨天: 1, 前天: 2 }[relative[1]!] ?? 0) * dayMs));
  }
  const absolute = /^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?(?:\s+[^\d\s]\S*)?$/u.exec(text);
  if (absolute === null) return undefined;
  let year = Number(absolute[1] ?? localNow.getUTCFullYear());
  const month = Number(absolute[2]);
  const date = Number(absolute[3]);
  const hour = Number(absolute[4] ?? 0);
  const minute = Number(absolute[5] ?? 0);
  // A yearless month/day denotes the most recent occurrence, including around New Year.
  if (absolute[1] === undefined && (month > localNow.getUTCMonth() + 1 || month === localNow.getUTCMonth() + 1 && date > localNow.getUTCDate())) year--;
  const check = new Date(Date.UTC(year, month - 1, date, hour, minute));
  if (year < 2000 || year > localNow.getUTCFullYear() || check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== date || hour > 23 || minute > 59) return undefined;
  return `${check.toISOString().slice(0, 19)}+08:00`;
}
