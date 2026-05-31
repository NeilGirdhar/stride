import { DAY } from "./ranges.js";

const HOUR = 60 * 60 * 1000;
const MONTH_NAMES = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];
const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function timeAxisLabels(
  start,
  end,
  width,
  { padL = 0, padR = 0, minPx = 78 } = {},
) {
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start)
    return [];

  const plotW = Math.max(1, width - padL - padR);
  const maxTicks = Math.max(2, Math.floor(plotW / minPx));
  const span = end - start;
  const interval = chooseInterval(span, maxTicks);
  let ticks = alignedTicks(start, end, interval);

  if (!ticks.length) ticks = [start, end];
  if (ticks.length === 1 && span > DAY) ticks.push(end);

  return ticks.map((t) => {
    const x = padL + (plotW * (t - start)) / span;
    return {
      t,
      label: tickLabel(t, interval.unit, span),
      anchor:
        x < padL + 20 ? "start" : x > width - padR - 20 ? "end" : "middle",
    };
  });
}

function chooseInterval(span, maxTicks) {
  const candidates = [
    { unit: "hour", step: 6, ms: 6 * HOUR },
    { unit: "hour", step: 12, ms: 12 * HOUR },
    { unit: "day", step: 1, ms: DAY },
    { unit: "day", step: 2, ms: 2 * DAY },
    { unit: "day", step: 7, ms: 7 * DAY },
    { unit: "day", step: 14, ms: 14 * DAY },
    { unit: "month", step: 1, ms: 30 * DAY },
    { unit: "month", step: 2, ms: 61 * DAY },
    { unit: "month", step: 3, ms: 91 * DAY },
    { unit: "month", step: 6, ms: 183 * DAY },
    { unit: "year", step: 1, ms: 365 * DAY },
  ];

  return (
    candidates.find(
      (candidate) => Math.ceil(span / candidate.ms) <= maxTicks,
    ) || candidates[candidates.length - 1]
  );
}

function alignedTicks(start, end, interval) {
  const ticks = [];
  const d = firstTick(start, interval);
  while (d.getTime() <= end) {
    const t = d.getTime();
    if (t >= start) ticks.push(t);
    advance(d, interval);
  }
  return ticks;
}

function firstTick(start, interval) {
  const d = new Date(start);
  d.setSeconds(0, 0);

  if (interval.unit === "hour") {
    d.setMinutes(0, 0, 0);
    d.setHours(Math.ceil(d.getHours() / interval.step) * interval.step);
    if (d.getTime() < start) d.setHours(d.getHours() + interval.step);
    return d;
  }

  d.setHours(0, 0, 0, 0);
  if (interval.unit === "day") {
    if (interval.step === 7 || interval.step === 14) {
      const daysToMonday = (8 - d.getDay()) % 7;
      d.setDate(d.getDate() + daysToMonday);
      if (interval.step === 14 && weekIndex(d) % 2) d.setDate(d.getDate() + 7);
    }
    if (d.getTime() < start) d.setDate(d.getDate() + interval.step);
    return d;
  }

  d.setDate(1);
  if (interval.unit === "month") {
    const month = d.getMonth();
    d.setMonth(Math.ceil(month / interval.step) * interval.step, 1);
    if (d.getTime() < start) d.setMonth(d.getMonth() + interval.step, 1);
    return d;
  }

  d.setMonth(0, 1);
  if (d.getTime() < start) d.setFullYear(d.getFullYear() + interval.step);
  return d;
}

function advance(d, interval) {
  if (interval.unit === "hour") d.setHours(d.getHours() + interval.step);
  else if (interval.unit === "day") d.setDate(d.getDate() + interval.step);
  else if (interval.unit === "month")
    d.setMonth(d.getMonth() + interval.step, 1);
  else d.setFullYear(d.getFullYear() + interval.step);
}

function tickLabel(t, unit, span) {
  const d = new Date(t);
  if (unit === "hour") {
    return d.toLocaleTimeString(undefined, { hour: "numeric" });
  }
  if (unit === "day") {
    if (span <= 21 * DAY) return `${WEEKDAY_NAMES[d.getDay()]} ${d.getDate()}`;
    return `${MONTH_NAMES[d.getMonth()]} ${d.getDate()}`;
  }
  if (unit === "month") {
    const month = MONTH_NAMES[d.getMonth()];
    if (d.getMonth() === 0 || span > 330 * DAY)
      return `${month} '${String(d.getFullYear()).slice(2)}`;
    return month;
  }
  return String(d.getFullYear());
}

function weekIndex(d) {
  const yearStart = new Date(d.getFullYear(), 0, 1).getTime();
  return Math.floor((d.getTime() - yearStart) / (7 * DAY));
}
