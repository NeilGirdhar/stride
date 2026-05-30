// Persistent state. localStorage-backed. Mutate via the exported `state` object,
// then call save(). bumpVersion of LS_KEY merges over previous shape — old data preserved.

const LS_KEY = 'sl-workout-plan-v4';
const LEGACY_KEYS = ['sl-workout-plan-v3'];

const defaultState = {
  profile: { name: '' },
  done: {},
  notes: {},
  paces: { current5k: { min: 34, sec: 19 }, goal5k: { min: 28, sec: 0 } },
  oura: { pat: '', lastReadiness: null, lastSync: null },
  strava: { clientId: '', endpoint: '/api/strava', accessToken: '', refreshToken: '', expiresAt: 0, lastActivities: [], lastSync: null },
  // NEW
  feel: {},          // 'wi-di' -> { feel, soreness, sleepHrs, sleepQuality, stress, ts }
  adjustments: {},   // wi -> { generatedAt, source, days: [{ dayIdx, action, original, replacement, reason }], notes }
  claude: { apiKey: '', model: 'claude-haiku-4-5-20251001' },
  voice: { enabled: true, voiceURI: '', rate: 1.0, volume: 1.0 },
  calendar: { data: null, clubs: null, lastSync: null },
};

function deepMerge(target, source) {
  const out = { ...target };
  for (const k of Object.keys(source || {})) {
    if (source[k] && typeof source[k] === 'object' && !Array.isArray(source[k]) && target[k] && typeof target[k] === 'object') {
      out[k] = deepMerge(target[k], source[k]);
    } else if (source[k] !== undefined) {
      out[k] = source[k];
    }
  }
  return out;
}

function loadFromStorage() {
  let base = JSON.parse(JSON.stringify(defaultState));
  try {
    let raw = localStorage.getItem(LS_KEY);
    if (!raw) {
      for (const legacy of LEGACY_KEYS) {
        const found = localStorage.getItem(legacy);
        if (found) { raw = found; break; }
      }
    }
    if (raw) base = deepMerge(base, JSON.parse(raw));
  } catch (e) { console.warn('Failed to load state:', e); }
  return base;
}

export const state = loadFromStorage();

export function save() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(state)); }
  catch (e) { console.warn('Failed to save state:', e); }
}

export function resetProgress() {
  state.done = {};
  state.notes = {};
  state.feel = {};
  state.adjustments = {};
  save();
}

export function exportAll() {
  return JSON.stringify(state, null, 2);
}

// Simple pub/sub so any module can subscribe to global re-render
const listeners = new Set();
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function notify() { for (const fn of listeners) try { fn(); } catch (e) { console.error(e); } }
