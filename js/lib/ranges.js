export const DAY = 86400000;

export const RANGE_ORDER = ["d30", "d60", "d90", "year", "serious", "all"];

export const RANGE_LABEL = {
  d30: "30d",
  d60: "60d",
  d90: "90d",
  year: "Year",
  serious: "Serious",
  all: "All",
};

export function rangeStart(view, { seriousStart, allStart, fallbackStart }) {
  const fallback = fallbackStart ?? allStart ?? seriousStart ?? Date.now();
  if (view === "d30") return Date.now() - 30 * DAY;
  if (view === "d60") return Date.now() - 60 * DAY;
  if (view === "d90") return Date.now() - 90 * DAY;
  if (view === "year") return Date.now() - 365 * DAY;
  if (view === "serious") return seriousStart ?? fallback;
  if (view === "all") return allStart ?? fallback;
  return fallback;
}

export function rangeButtonsHTML({ active, seriousStart }) {
  return RANGE_ORDER.map(
    (view) =>
      `<button class="rg-btn ${view === active ? "on" : ""}" data-view="${view}"${
        view === "serious" && seriousStart
          ? ` title="Since start of serious running · ${fmtDate(seriousStart)}"`
          : ""
      }>${RANGE_LABEL[view]}</button>`,
  ).join("");
}

export function parseLocalDate(value) {
  const [year, month, day] = String(value).split("-").map(Number);
  return new Date(year, month - 1, day).getTime();
}

export function fmtDate(t) {
  return new Date(t).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
