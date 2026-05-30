// Settings pane — minimal for now (profile name). Connections, paces, export,
// and the Strava sync status will move here as the rebuild continues.

import { state, save } from '../state.js';

export function renderSettings() {
  const root = document.getElementById('page-settings');
  if (!root) return;
  root.innerHTML = `
    <div class="hero">
      <div class="eyebrow">Settings</div>
      <h1>Settings</h1>
    </div>
    <div class="card">
      <div class="field">
        <label>Your name</label>
        <input type="text" id="profile-name" placeholder="Shown on your stats" autocomplete="name">
      </div>
    </div>
    <div class="card stub-card">
      <p class="stub-meta">Strava sync, training paces, calendar export and connections will return here as panes come online.</p>
    </div>`;

  const name = root.querySelector('#profile-name');
  name.value = state.profile.name;
  name.oninput = () => { state.profile.name = name.value; save(); };
}
