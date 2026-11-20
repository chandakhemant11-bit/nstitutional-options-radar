DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Institutional Options Radar</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', system-ui, sans-serif; }
  body { background: #0b0e14; color: #e2e8f0; padding: 14px; }
  
  /* Top Alert Banner */
  .alert-bar {
    padding: 14px 20px; border-radius: 8px; margin-bottom: 14px;
    display: flex; justify-content: space-between; align-items: center;
    font-weight: 700; font-size: 16px; transition: all 0.3s ease;
    border: 1px solid #1e293b; background: #0f291e; color: #34d399;
  }
  .alert-bar.danger {
    background: #991b1b; color: #ffffff; border-color: #f87171;
    animation: pulseRed 1s infinite;
  }
  @keyframes pulseRed {
    0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }
    70% { box-shadow: 0 0 0 12px rgba(239, 68, 68, 0); }
    100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
  }

  /* Navigation Tabs */
  .tabs { display: flex; gap: 10px; margin-bottom: 14px; }
  .tab-btn {
    padding: 10px 22px; background: #151b26; color: #94a3b8; border: 1px solid #1e293b;
    border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 14px;
  }
  .tab-btn.active { background: #2563eb; color: #fff; border-color: #3b82f6; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* Tab 1: 3-Box Grid */
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
  .card {
    background: #131722; border: 1px solid #1e293b; border-radius: 8px; padding: 16px;
  }
  .card-title { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #64748b; margin-bottom: 12px; font-weight: 700; }
  .index-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #1e293b; font-size: 15px; }
  .index-row:last-child { border-bottom: none; }
  .pos { color: #10b981; font-weight: 700; }
  .neg { color: #ef4444; font-weight: 700; }

  /* Signal Box */
  .signal-badge {
    display: inline-block; padding: 8px 16px; border-radius: 6px; font-size: 18px; font-weight: 800;
    background: #064e3b; color: #34d399; margin-bottom: 10px;
  }
  .signal-badge.exit { background: #7f1d1d; color: #fca5a5; }
  .note-box {
    background: #0f172a; border-left: 4px solid #3b82f6; padding: 12px; border-radius: 4px;
    font-size: 14px; line-height: 1.5; color: #cbd5e1;
  }

  /* Tab 2: Table */
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #1e293b; }
  th { color: #94a3b8; font-size: 12px; text-transform: uppercase; }
  .btn-sound {
    background: #1e293b; color: #e2e8f0; border: 1px solid #334155; padding: 6px 12px;
    border-radius: 4px; cursor: pointer; font-size: 12px;
  }
</style>
</head>
<body>

  <!-- TOP ALERT BAR -->
  <div id="topAlert" class="alert-bar">
    <span id="alertText">🟢 SYSTEM ARMED — NOISE-FREE RADAR ACTIVE</span>
    <div>
      <span id="clock" style="margin-right:15px; font-size:13px; color:#94a3b8;">--:--:--</span>
      <button class="btn-sound" onclick="enableAudio()">🔊 Enable Audio Siren</button>
    </div>
  </div>

  <!-- TABS -->
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('tab1', this)">TAB 1: EXECUTION & PROTECTION (CLEAN)</button>
    <button class="tab-btn" onclick="switchTab('tab2', this)">TAB 2: INSTITUTIONAL X-RAY (DEEP DATA)</button>
  </div>

  <!-- TAB 1 -->
  <div id="tab1" class="tab-content active">
    <div class="grid-2">
      <!-- BOX 1 -->
      <div class="card">
        <div class="card-title">Box 1: Live Indices & Heavyweight Impulse</div>
        <div id="indicesContainer">Connecting to Shoonya Tick Stream...</div>
      </div>
      <!-- BOX 2 -->
      <div class="card">
        <div class="card-title">Box 2: Intraday & Positional Action Signal</div>
        <div id="mainSignal" class="signal-badge">CALM — SAFE FOR THETA SELLING</div>
        <div id="positionalStatus" style="font-size:14px; color:#94a3b8; margin-top:6px;">
          Positional Horizon: Monitoring Multi-Day Compression...
        </div>
      </div>
    </div>

    <!-- BOX 3 -->
    <div class="card">
      <div class="card-title">Box 3: Live Stock Impact & Scalping Note (Noise-Filtered)</div>
      <div id="liveNote" class="note-box">Waiting for institutional volume burst in top 12 heavyweights...</div>
    </div>
  </div>

  <!-- TAB 2 -->
  <div id="tab2" class="tab-content">
    <div class="card">
      <div class="card-title">Top 12 Heavyweights — Millisecond Attribution & Volume Burst Matrix</div>
      <table>
        <thead>
          <tr>
            <th>Stock</th>
            <th>LTP</th>
            <th>Day %</th>
            <th>5s Impulse %</th>
            <th>Nifty Pts Impact</th>
            <th>Tick Count</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="xrayTable"></tbody>
      </table>
    </div>
  </div>

<script>
  let audioCtx = null;
  let lastAlertId = "";

  function enableAudio() {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    playSiren(1);
    alert("Audio Siren Enabled! Bada move aate hi speaker par alarm bajega.");
  }

  function playSiren(beeps = 3) {
    if (!audioCtx) return;
    for (let i = 0; i < beeps; i++) {
      let osc = audioCtx.createOscillator();
      let gain = audioCtx.createGain();
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(980, audioCtx.currentTime + i * 0.4);
      osc.frequency.linearRampToValueAtTime(1450, audioCtx.currentTime + i * 0.4 + 0.2);
      gain.gain.setValueAtTime(0.2, audioCtx.currentTime + i * 0.4);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start(audioCtx.currentTime + i * 0.4);
      osc.stop(audioCtx.currentTime + i * 0.4 + 0.3);
    }
  }

  function switchTab(tabId, btn) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    btn.classList.add('active');
  }

  async function fetchState() {
    try {
      const res = await fetch('/api/state');
      const data = await res.json();
      document.getElementById('clock').innerText = data.timestamp + " | Ticks: " + data.total_ticks;

      // Update Indices
      let idxHtml = "";
      for (const [name, info] of Object.entries(data.indices)) {
        let cls = info.pct >= 0 ? 'pos' : 'neg';
        idxHtml += `<div class="index-row">
          <span><strong>${name}</strong></span>
          <span>${info.ltp.toFixed(2)} <span class="${cls}">(${info.pct >= 0 ? '+' : ''}${info.pct.toFixed(2)}%)</span></span>
        </div>`;
      }
      document.getElementById('indicesContainer').innerHTML = idxHtml;

      // Update Alert & Signal
      const alertBar = document.getElementById('topAlert');
      const sigEl = document.getElementById('mainSignal');
      if (data.alert.active) {
        alertBar.classList.add('danger');
        document.getElementById('alertText').innerText = "🚨 " + data.alert.title;
        sigEl.className = "signal-badge exit";
        sigEl.innerText = data.alert.action;
        if (data.alert.id !== lastAlertId) {
          lastAlertId = data.alert.id;
          playSiren(3);
        }
      } else {
        alertBar.classList.remove('danger');
        document.getElementById('alertText').innerText = "🟢 CALM REGIME — SAFE FOR THETA OPTION SELLING";
        sigEl.className = "signal-badge";
        sigEl.innerText = "HOLD / SELL THETA (NO INSTITUTIONAL SPIKE)";
      }

      document.getElementById('liveNote').innerHTML = data.note;

      // Update Tab 2 X-Ray Table
      let rows = "";
      data.stocks.forEach(s => {
        let dayCls = s.day_pct >= 0 ? 'pos' : 'neg';
        let impCls = s.imp_pct >= 0 ? 'pos' : 'neg';
        rows += `<tr>
          <td><strong>${s.name}</strong></td>
          <td>${s.ltp.toFixed(2)}</td>
          <td class="${dayCls}">${s.day_pct >= 0 ? '+' : ''}${s.day_pct.toFixed(2)}%</td>
          <td class="${impCls}">${s.imp_pct >= 0 ? '+' : ''}${s.imp_pct.toFixed(2)}%</td>
          <td class="${impCls}">${s.pts >= 0 ? '+' : ''}${s.pts.toFixed(1)} pts</td>
          <td>${s.ticks}</td>
          <td>${s.status}</td>
        </tr>`;
      });
      document.getElementById('xrayTable').innerHTML = rows;
    } catch (e) {}
  }

  setInterval(fetchState, 800); // Smooth 800ms refresh
</script>
</body>
</html>
"""
