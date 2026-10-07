export function localDateKey(date) {
  if (!(date instanceof Date) || !Number.isFinite(date.getTime())) return null;
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function isValidLocalDateKey(value) {
  const match = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1) return false;
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= daysInMonth[month - 1];
}

export function taskDueLocalDate(dueAt) {
  if (dueAt instanceof Date) return localDateKey(dueAt);
  if (typeof dueAt !== "string" || !dueAt.trim()) return null;

  const value = dueAt.trim();
  if (isValidLocalDateKey(value)) return value;

  const datePrefix = /^(\d{4})-(\d{2})-(\d{2})(?:[Tt ]|$)/.exec(value);
  if (datePrefix && !isValidLocalDateKey(`${datePrefix[1]}-${datePrefix[2]}-${datePrefix[3]}`)) return null;

  const parsed = new Date(value);
  return localDateKey(parsed);
}

export function positiveEstimateMinutes(value) {
  let estimate;
  try {
    estimate = Number(value);
  } catch {
    return 0;
  }
  return Number.isFinite(estimate) && estimate > 0 ? estimate : 0;
}

export function getDailyWorkloadEstimate(task, today) {
  if (!task || (task.status !== "todo" && task.status !== "doing") || !isValidLocalDateKey(today)) return null;
  const dueDate = taskDueLocalDate(task.due_at);
  if (!dueDate || dueDate > today) return null;

  return positiveEstimateMinutes(task.estimated_minutes);
}

export function filterUnestimatedDailyWorkloadTasks(tasks, now = new Date()) {
  const today = localDateKey(now);
  if (!today || !Array.isArray(tasks)) return [];
  return tasks.filter((task) => getDailyWorkloadEstimate(task, today) === 0);
}
