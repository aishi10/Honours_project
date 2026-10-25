"""Local interactive web dashboard for the QoS simulator.

Run from this directory with: python3 dashboard.py
Then open http://127.0.0.1:8000 in a browser.
Uses only Python's standard library and qos_simulator.py.
"""

from __future__ import annotations

import csv
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from qos_simulator import (
    BURSTY_FLOWS,
    FLOWS,
    LINK_RATE_BPS,
    QUEUE_LIMIT_PACKETS,
    SIMULATION_TIME_S,
    SCHEDULERS,
    VOICE_CONGESTION_FLOWS,
    simulate,
)

HOST = "127.0.0.1"
PORT = 8000
SCENARIOS = {
    "default": ("Default traffic", FLOWS),
    "voice": ("Congested voice", VOICE_CONGESTION_FLOWS),
    "bursty": ("Bursty traffic", BURSTY_FLOWS),
}
SHAPERS = ("None", "Token Bucket", "Leaky Bucket")


def _option(value: str, selected: str, label: str | None = None) -> str:
    mark = " selected" if value == selected else ""
    return f'<option value="{escape(value)}"{mark}>{escape(label or value)}</option>'


def _render_form(values: dict[str, str]) -> str:
    scenario_options = "".join(
        _option(key, values["scenario"], title) for key, (title, _) in SCENARIOS.items()
    )
    scheduler_options = "".join(_option(name, values["scheduler"]) for name in SCHEDULERS)
    shaper_options = "".join(_option(name, values["shaper"]) for name in SHAPERS)
    compare_checked = " checked" if values.get("compare_all") == "on" else ""
    return f"""
    <div class="preset-bar" aria-label="Experiment presets">
      <strong>Quick presets</strong>
      <button type="button" class="preset-button" data-preset="voice">Protect voice</button>
      <button type="button" class="preset-button" data-preset="fairness">Compare fairness</button>
      <button type="button" class="preset-button" data-preset="bursts">Smooth bursts</button>
    </div>
    <form method="post" action="/">
      <label>Traffic profile<select name="scenario">{scenario_options}</select></label>
      <label>Scheduler<select name="scheduler">{scheduler_options}</select></label>
      <label>Traffic shaper<select name="shaper">{shaper_options}</select></label>
      <label>Shaper rate (Mbps per flow)<input name="rate" type="number" min="0.1" max="100" step="0.1" value="{escape(values['rate'])}"></label>
      <label>Bucket / buffer (Mb per flow)<input name="capacity" type="number" min="0.01" max="50" step="0.01" value="{escape(values['capacity'])}"></label>
      <label class="check"><input name="compare_all" type="checkbox"{compare_checked}> Compare all schedulers</label>
      <button type="submit">Run simulation</button>
    </form>
    """


def _render_result(name: str, results: dict[str, dict[str, float | int]]) -> str:
    flow_rows = []
    for flow, row in results.items():
        if flow.startswith("_"):
            continue
        throughput = float(row["throughput_mbps"])
        bar_width = min(100.0, throughput / (LINK_RATE_BPS / 1_000_000) * 100)
        flow_rows.append(f"""
          <tr>
            <th>{escape(flow.title())}</th>
            <td>{row['generated_packets']}</td>
            <td>{row['transmitted_packets']}</td>
            <td>{row['dropped_packets']}</td>
            <td>{row['shaper_dropped_packets']}</td>
            <td>{float(row['packet_loss_percent']):.2f}%</td>
            <td><div class="bar-wrap"><span style="width:{bar_width:.2f}%"></span></div>{throughput:.3f} Mbps</td>
            <td>{float(row['average_delay_ms']):.3f}</td>
            <td>{float(row['average_jitter_ms']):.3f}</td>
          </tr>
        """)
    queue = results["_queue"]
    fairness = float(results["_summary"]["jain_fairness_index"])
    adaptive_card = ""
    if "adaptive_max_voice_weight" in results["_summary"]:
        adaptive_card = f"<div><small>Adaptive voice weight</small><strong>{results['_summary']['adaptive_max_voice_weight']}<em> max · {results['_summary']['adaptive_weight_changes']} changes</em></strong></div>"
    return f"""
      <section class="result">
        <h2>{escape(name)}</h2>
        <div class="cards">
          <div><small>Jain throughput fairness</small><strong>{fairness:.3f}<em> / 1.000</em></strong></div>
          <div><small>Average network queue</small><strong>{float(queue['average_queue_packets']):.1f}<em> packets</em></strong></div>
          <div><small>Peak network queue</small><strong>{queue['maximum_queue_packets']}<em> packets</em></strong></div>
          <div><small>Queue limit</small><strong>{queue['queue_limit_packets']}<em> per flow</em></strong></div>
          {adaptive_card}
        </div>
        <div class="table-scroll"><table>
          <thead><tr><th>Flow</th><th>Generated</th><th>Sent*</th><th>Network drop</th><th>Shaper drop</th><th>Loss</th><th>Throughput</th><th>Delay (ms)</th><th>Jitter (ms)</th></tr></thead>
          <tbody>{''.join(flow_rows)}</tbody>
        </table></div>
      </section>
    """


def _render_live_demo() -> str:
    return """
    <section class="panel demo-panel">
      <div class="demo-heading">
        <div><div class="eyebrow dark">Packet trace replay</div><h2>Traffic through the QoS router</h2></div>
        <div class="demo-controls"><button type="button" id="demo-start">Replay actual packets</button><button type="button" class="secondary" id="demo-pause">Pause</button></div>
      </div>
      <div class="diagram-scroll">
        <svg id="traffic-diagram" viewBox="0 0 1100 350" role="img" aria-label="Animated voice, video, and file packets flowing through per-flow shapers and queues into a shared scheduler and network link">
          <defs>
            <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#9aabc3"/></marker>
          </defs>
          <path id="route-voice" class="route" d="M150 75 H216 H354 H440 C500 75 540 130 615 175 H800 H965"/>
          <path id="route-video" class="route" d="M150 175 H216 H354 H440 H615 H800 H965"/>
          <path id="route-file" class="route" d="M150 275 H216 H354 H440 C500 275 540 220 615 175 H800 H965"/>
          <g class="flow-node voice-node"><rect x="12" y="50" width="138" height="50" rx="10"/><text x="81" y="72">VOICE</text><text class="sub" x="81" y="89">latency sensitive</text></g>
          <g class="flow-node video-node"><rect x="12" y="150" width="138" height="50" rx="10"/><text x="81" y="172">VIDEO</text><text class="sub" x="81" y="189">high bandwidth</text></g>
          <g class="flow-node file-node"><rect x="12" y="250" width="138" height="50" rx="10"/><text x="81" y="272">FILE</text><text class="sub" x="81" y="289">best effort</text></g>
          <g class="stage"><rect x="216" y="38" width="138" height="274" rx="13"/><text x="285" y="23">PER-FLOW SHAPERS</text>
            <rect class="slot" x="228" y="55" width="114" height="40" rx="7"/><text class="slot-text" x="285" y="80">Voice shaper</text>
            <rect class="slot" x="228" y="155" width="114" height="40" rx="7"/><text class="slot-text" x="285" y="180">Video shaper</text>
            <rect class="slot" x="228" y="255" width="114" height="40" rx="7"/><text class="slot-text" x="285" y="280">File shaper</text>
          </g>
          <g class="stage"><rect x="440" y="38" width="138" height="274" rx="13"/><text x="509" y="23">FLOW QUEUES</text>
            <rect class="slot" x="452" y="55" width="114" height="40" rx="7"/><text class="slot-text" x="509" y="80">Voice queue</text>
            <rect class="slot" x="452" y="155" width="114" height="40" rx="7"/><text class="slot-text" x="509" y="180">Video queue</text>
            <rect class="slot" x="452" y="255" width="114" height="40" rx="7"/><text class="slot-text" x="509" y="280">File queue</text>
          </g>
          <g class="shared"><rect x="615" y="105" width="145" height="140" rx="14"/><text x="687" y="153">SCHEDULER</text><text id="demo-scheduler" class="shared-sub" x="687" y="178">FIFO</text><text class="shared-sub" x="687" y="207">select next packet</text></g>
          <g class="shared"><rect x="800" y="115" width="132" height="120" rx="14"/><text x="866" y="159">BOTTLENECK</text><text class="shared-sub" x="866" y="184">10 Mbps link</text><text class="shared-sub" x="866" y="207">shared capacity</text></g>
          <g class="receiver"><rect x="965" y="150" width="123" height="50" rx="11"/><text x="1026" y="180">RECEIVER</text></g>
          <g id="packet-layer"></g>
        </svg>
      </div>
      <div class="demo-status"><span id="demo-mode">Default traffic · FIFO · no shaper</span><span id="demo-telemetry">Ready to build a sampled packet trace.</span></div>
      <p class="footnote">Packets are sampled from an actual simulator run. Their animation is slowed for viewing; queue occupancy, drops, scheduling order, and Adaptive WRR weights come from recorded events.</p>
      <section class="queue-history" aria-labelledby="queue-heading">
        <h3 id="queue-heading">Network queue history</h3>
        <svg id="queue-chart" viewBox="0 0 800 250" role="img" aria-label="Waiting network queue depth over simulated time, synchronized with packet replay">
          <text x="400" y="125" text-anchor="middle" fill="#607089">Replay packets to load queue history</text>
        </svg>
      </section>
    </section>
    <script>
    (() => {
      const svg = document.getElementById("traffic-diagram");
      const layer = document.getElementById("packet-layer");
      const scenario = document.querySelector('[name="scenario"]');
      const scheduler = document.querySelector('[name="scheduler"]');
      const shaper = document.querySelector('[name="shaper"]');
      const shaperRate = document.querySelector('[name="rate"]');
      const bucketCapacity = document.querySelector('[name="capacity"]');
      const mode = document.getElementById("demo-mode");
      const telemetry = document.getElementById("demo-telemetry");
      const queueChart = document.getElementById("queue-chart");
      const routes = {
        voice: {color:"#e65b48", path:document.getElementById("route-voice")},
        video: {color:"#3676db", path:document.getElementById("route-video")},
        file: {color:"#23a57b", path:document.getElementById("route-file")}
      };
      let tracks = [];
      let replayEvents = [];
      let eventIndex = 0;
      let replayStart = 0;
      let firstSimTime = 0;
      let lastSimTime = 0;
      let running = false;
      let frameId = 0;
      let totalNetworkDrops = 0;
      let totalShaperDrops = 0;
      let queueChartEnd = 10;
      let queueCursor = null;

      const presets = {
        voice: {scenario:"voice", scheduler:"Priority", shaper:"None", rate:"5", capacity:"1", compare:false},
        fairness: {scenario:"voice", scheduler:"Adaptive WRR", shaper:"None", rate:"5", capacity:"1", compare:true},
        bursts: {scenario:"bursty", scheduler:"Priority", shaper:"Leaky Bucket", rate:"5", capacity:"1", compare:false}
      };

      function drawQueueChart(points) {
        const width = 800, height = 250;
        const left = 66, right = 780, top = 18, bottom = 195;
        queueChart.replaceChildren();
        if (!points || points.length < 2) return;
        queueChartEnd = Math.max(0.01, Number(points[points.length - 1].time_s));
        const maximum = Math.max(...points.map(point => Number(point.queue_packets)));
        const yMax = Math.max(5, Math.ceil(maximum / 10) * 10);
        const x = value => left + Math.max(0, Math.min(queueChartEnd, value)) / queueChartEnd * (right - left);
        const y = value => bottom - value / yMax * (bottom - top);
        const add = (tag, attrs, content) => {
          const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
          for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
          if (content !== undefined) element.textContent = content;
          queueChart.appendChild(element);
          return element;
        };
        const xTicks = 5;
        for (let tick = 0; tick <= xTicks; tick++) {
          const value = queueChartEnd * tick / xTicks;
          const tickX = x(value);
          add("line", {x1:tickX,y1:top,x2:tickX,y2:bottom,stroke:"#e1e6ee"});
          add("text", {x:tickX,y:bottom+20,"text-anchor":"middle",fill:"#607089","font-size":"11"}, value.toFixed(1));
        }
        for (let tick = 0; tick <= 4; tick++) {
          const value = yMax * tick / 4;
          const tickY = y(value);
          add("line", {x1:left,y1:tickY,x2:right,y2:tickY,stroke:"#e1e6ee"});
          add("text", {x:left-10,y:tickY+4,"text-anchor":"end",fill:"#607089","font-size":"11"}, Math.round(value));
        }
        let path = `M ${x(points[0].time_s)} ${y(points[0].queue_packets)}`;
        for (let index = 1; index < points.length; index++) {
          path += ` H ${x(points[index].time_s)} V ${y(points[index].queue_packets)}`;
        }
        add("path", {d:path,fill:"none",stroke:"#315fd1","stroke-width":"2"});
        add("text", {x:(left+right)/2,y:height-4,"text-anchor":"middle",fill:"#35445d","font-size":"12"}, "Simulation time (s)");
        add("text", {x:15,y:(top+bottom)/2,transform:`rotate(-90 15 ${(top+bottom)/2})`,"text-anchor":"middle",fill:"#35445d","font-size":"12"}, "Waiting packets");
        queueCursor = add("line", {x1:left,y1:top,x2:left,y2:bottom,stroke:"#e15759","stroke-width":"2"});
      }

      function configure() {
        const selectedShaper = shaper.value;
        const rate = Number(shaperRate.value);
        const capacity = Number(bucketCapacity.value);
        running = false;
        cancelAnimationFrame(frameId);
        tracks = [];
        replayEvents = [];
        layer.replaceChildren();
        document.getElementById("demo-scheduler").textContent = scheduler.value;
        const scenarioName = scenario.options[scenario.selectedIndex].text;
        mode.textContent = scenarioName + " · " + scheduler.value + " · " + selectedShaper +
          (selectedShaper === "None" ? "" : " · " + rate.toFixed(1) + " Mbps / " + capacity.toFixed(2) + " Mb");
        telemetry.textContent = "Ready to build a sampled packet trace.";
      }

      function buildTracks(events) {
        const grouped = new Map();
        for (const event of events) {
          if (!grouped.has(event.packet_id)) grouped.set(event.packet_id, []);
          grouped.get(event.packet_id).push(event);
        }
        const position = {source:0.015, shaper_wait:0.20, shaper_release:0.34,
                          enqueue:0.50, network_drop:0.53, service_start:0.72, transmit:0.985,
                          shaper_drop:0.25};
        const result = [];
        for (const [id, packetEvents] of grouped) {
          packetEvents.sort((a,b) => a.time_s - b.time_s);
          const flow = packetEvents[0].flow;
          const route = routes[flow];
          const stages = [];
          let displayTime = (packetEvents[0].time_s - firstSimTime) / 2.5;
          let priorEventTime = packetEvents[0].time_s;
          for (let index = 0; index < packetEvents.length; index++) {
            const event = packetEvents[index];
            const eventGap = Math.max(0, event.time_s - priorEventTime) / 2.5;
            displayTime += index === 0 ? 0 : Math.max(0.075, eventGap);
            stages.push({time:displayTime, progress:position[event.event] ?? 0.5,
                         event:event.event, queue:event.network_queue_packets,
                         weight:event.voice_weight, simTime:event.time_s});
            priorEventTime = event.time_s;
          }
          result.push({id, flow, route, stages, endTime:displayTime});
        }
        return result;
      }

      function setPoint(dot, route, progress) {
        const point = route.path.getPointAtLength(Math.max(0, Math.min(1, progress)) * route.path.getTotalLength());
        dot.setAttribute("cx", point.x);
        dot.setAttribute("cy", point.y);
      }

      function renderReplay(timestamp) {
        const playbackTime = (timestamp - replayStart) / 1000;
        const simTime = firstSimTime + playbackTime * 2.5;
        let queuePackets = 0;
        let voiceWeight = 3;
        while (eventIndex < replayEvents.length && replayEvents[eventIndex].time_s <= simTime) {
          const event = replayEvents[eventIndex++];
          queuePackets = event.network_queue_packets;
          if (event.voice_weight) voiceWeight = event.voice_weight;
          if (event.event === "network_drop") totalNetworkDrops++;
          if (event.event === "shaper_drop") totalShaperDrops++;
        }
        const eventTime = eventIndex ? replayEvents[eventIndex - 1].time_s : firstSimTime;
        const lastEvent = eventIndex ? replayEvents[eventIndex - 1] : null;
        if (lastEvent) {
          queuePackets = lastEvent.network_queue_packets;
          if (lastEvent.voice_weight) voiceWeight = lastEvent.voice_weight;
        }
        for (const track of tracks) {
          const time = playbackTime;
          if (time > track.endTime + 0.3) { track.dot.style.opacity = "0"; continue; }
          let left = track.stages[0];
          let right = null;
          for (let i = 1; i < track.stages.length; i++) {
            if (time <= track.stages[i].time) { right = track.stages[i]; break; }
            left = track.stages[i];
          }
          if (right) {
            const span = Math.max(0.001, right.time - left.time);
            const phase = Math.max(0, Math.min(1, (time - left.time) / span));
            setPoint(track.dot, track.route, left.progress + (right.progress - left.progress) * phase);
          } else {
            setPoint(track.dot, track.route, left.progress);
          }
          const dropped = track.stages.some(stage => stage.event === "network_drop" || stage.event === "shaper_drop");
          track.dot.setAttribute("fill", dropped ? "#d73737" : track.route.color);
          track.dot.style.opacity = time < track.stages[0].time ? "0" : "0.96";
        }
        if (queueCursor) {
          const cursorX = 66 + Math.max(0, Math.min(queueChartEnd, simTime)) / queueChartEnd * (780 - 66);
          queueCursor.setAttribute("x1", cursorX);
          queueCursor.setAttribute("x2", cursorX);
        }
        telemetry.textContent = "Sim " + eventTime.toFixed(3) + " s · network queue " + queuePackets +
          " packets · voice WRR weight " + voiceWeight + " · sampled drops " + totalNetworkDrops +
          " network / " + totalShaperDrops + " shaper";
        if (eventIndex < replayEvents.length || playbackTime < Math.max(...tracks.map(t => t.endTime), 0) + 0.4) {
          frameId = requestAnimationFrame(renderReplay);
        } else {
          running = false;
          telemetry.textContent += " · replay complete";
        }
      }

      async function startReplay() {
        running = false;
        cancelAnimationFrame(frameId);
        layer.replaceChildren();
        telemetry.textContent = "Running simulator and preparing packet trace…";
        const form = document.querySelector("form");
        const params = new URLSearchParams(new FormData(form));
        params.delete("compare_all");
        try {
          const response = await fetch("/trace?" + params.toString(), {cache:"no-store"});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "Unable to create trace.");
          replayEvents = payload.events;
          if (!replayEvents.length) throw new Error("No packet events were recorded for this selection.");
          firstSimTime = replayEvents[0].time_s;
          drawQueueChart(payload.queue_history);
          eventIndex = 0;
          totalNetworkDrops = 0;
          totalShaperDrops = 0;
          tracks = buildTracks(replayEvents);
          for (const track of tracks) {
            const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            dot.setAttribute("r", track.flow === "voice" ? "5.5" : "6");
            dot.setAttribute("fill", track.route.color);
            dot.setAttribute("class", "packet-dot trace-packet");
            dot.style.opacity = "0";
            layer.appendChild(dot);
            track.dot = dot;
          }
          running = true;
          replayStart = performance.now();
          telemetry.textContent = payload.event_count + " recorded events · replaying at 2.5× simulation time";
          frameId = requestAnimationFrame(renderReplay);
        } catch (error) {
          telemetry.textContent = "Replay error: " + error.message;
        }
      }

      document.getElementById("demo-start").addEventListener("click", startReplay);
      document.getElementById("demo-pause").addEventListener("click", () => {
        running = false;
        cancelAnimationFrame(frameId);
      });
      document.querySelectorAll("[data-preset]").forEach(button => button.addEventListener("click", () => {
        const preset = presets[button.dataset.preset];
        scenario.value = preset.scenario;
        scheduler.value = preset.scheduler;
        shaper.value = preset.shaper;
        shaperRate.value = preset.rate;
        bucketCapacity.value = preset.capacity;
        document.querySelector('[name="compare_all"]').checked = preset.compare;
        configure();
        telemetry.textContent = "Preset loaded. Run simulation or replay packets to see the result.";
      }));
      [scenario, scheduler, shaper, shaperRate, bucketCapacity].forEach(control => control.addEventListener("change", configure));
      [shaperRate, bucketCapacity].forEach(control => control.addEventListener("input", configure));
      configure();
    })();
    </script>
    """


def _render_results_explorer() -> str:
    return """
    <section class="panel results-explorer" aria-labelledby="explorer-heading">
      <div class="eyebrow dark">20 seeded Poisson runs per setting</div>
      <h2 id="explorer-heading">Stochastic results explorer</h2>
      <p>Compare mean results and 95% confidence intervals across traffic profiles, shapers, and schedulers.</p>
      <div class="explorer-controls">
        <label>Traffic profile<select id="explorer-scenario">
          <option value="Default traffic">Default traffic</option>
          <option value="Congested voice">Congested voice</option>
          <option value="Changing traffic">Changing traffic</option>
          <option value="Bursty traffic">Bursty traffic</option>
        </select></label>
        <label>Traffic shaper<select id="explorer-shaper">
          <option value="None">None</option>
          <option value="Token Bucket">Token Bucket</option>
          <option value="Leaky Bucket">Leaky Bucket</option>
        </select></label>
        <label>Flow<select id="explorer-flow">
          <option value="voice">Voice</option>
          <option value="video">Video</option>
          <option value="file">File</option>
        </select></label>
        <label>Measure<select id="explorer-metric">
          <option value="packet_loss_percent">Packet loss</option>
          <option value="average_delay_ms">Average delay</option>
          <option value="average_jitter_ms">Average jitter</option>
          <option value="throughput_mbps">Throughput</option>
          <option value="average_queue_packets">Average queue</option>
          <option value="jain_fairness_index">Jain fairness</option>
        </select></label>
      </div>
      <p id="explorer-status" class="meta" aria-live="polite">Loading precomputed stochastic results…</p>
      <svg id="explorer-chart" viewBox="0 0 900 350" role="img" aria-label="Scheduler comparison with means and 95 percent confidence intervals"></svg>
    </section>
    <script>
    (() => {
      const chart = document.getElementById("explorer-chart");
      const status = document.getElementById("explorer-status");
      const scenario = document.getElementById("explorer-scenario");
      const shaper = document.getElementById("explorer-shaper");
      const flow = document.getElementById("explorer-flow");
      const metric = document.getElementById("explorer-metric");
      const schedulerNames = ["FIFO", "Priority", "Round Robin", "Weighted Round Robin", "Adaptive WRR"];
      const schedulerColors = {"FIFO":"#4e79a7", "Priority":"#e15759", "Round Robin":"#59a14f",
        "Weighted Round Robin":"#f28e2b", "Adaptive WRR":"#8064a2"};
      const metricInfo = {
        packet_loss_percent:{label:"Packet loss",unit:"%",digits:2,max:100},
        average_delay_ms:{label:"Average delay",unit:"ms",digits:2},
        average_jitter_ms:{label:"Average jitter",unit:"ms",digits:2},
        throughput_mbps:{label:"Throughput",unit:"Mbps",digits:3},
        average_queue_packets:{label:"Average queue",unit:"packets",digits:1},
        jain_fairness_index:{label:"Jain fairness",unit:"index",digits:3,max:1}
      };
      let rows = [];

      function draw() {
        chart.replaceChildren();
        const chosenMetric = metric.value;
        const info = metricInfo[chosenMetric];
        const chosenFlow = chosenMetric === "jain_fairness_index" ? "voice" : flow.value;
        const filtered = rows.filter(row => row.scenario === scenario.value && row.shaper === shaper.value &&
          row.flow === chosenFlow).sort((a,b) => schedulerNames.indexOf(a.scheduler) - schedulerNames.indexOf(b.scheduler));
        if (!filtered.length) {
          status.textContent = "No stochastic results are available for this selection.";
          return;
        }
        const values = filtered.map(row => ({
          name:row.scheduler,
          mean:Number(row[chosenMetric + "_mean"]),
          low:Number(row[chosenMetric + "_ci95_low"]),
          high:Number(row[chosenMetric + "_ci95_high"]),
          seeds:Number(row.seed_runs)
        }));
        let xMax = info.max || Math.max(...values.map(value => value.high), 1);
        if (!info.max) {
          const rawStep = xMax / 4;
          const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
          const normalized = rawStep / magnitude;
          const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
          xMax = nice * magnitude * 4;
        }
        const left = 190, right = 845, top = 42, rowGap = 52;
        const x = value => left + Math.max(0, Math.min(xMax, value)) / xMax * (right-left);
        const add = (tag, attrs, text) => {
          const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
          for (const [key,value] of Object.entries(attrs)) item.setAttribute(key,value);
          if (text !== undefined) item.textContent = text;
          chart.appendChild(item);
          return item;
        };
        for (let tick=0; tick<=4; tick++) {
          const value=xMax*tick/4, tickX=x(value);
          add("line",{x1:tickX,y1:top-12,x2:tickX,y2:top+rowGap*values.length-14,stroke:"#e1e6ee"});
          add("text",{x:tickX,y:top+rowGap*values.length+8,"text-anchor":"middle",fill:"#607089","font-size":"12"},value.toFixed(info.digits));
        }
        values.forEach((value,index) => {
          const center=top+index*rowGap+12, meanX=x(value.mean), lowX=x(value.low), highX=x(value.high);
          add("text",{x:left-14,y:center+5,"text-anchor":"end",fill:"#172033","font-size":"13"},value.name);
          add("rect",{x:left,y:center-8,width:Math.max(1,meanX-left),height:16,fill:schedulerColors[value.name],opacity:"0.8"});
          add("line",{x1:lowX,y1:center,x2:highX,y2:center,stroke:"#172033","stroke-width":"2"});
          add("line",{x1:lowX,y1:center-6,x2:lowX,y2:center+6,stroke:"#172033","stroke-width":"1.5"});
          add("line",{x1:highX,y1:center-6,x2:highX,y2:center+6,stroke:"#172033","stroke-width":"1.5"});
          add("text",{x:right+10,y:center+4,fill:"#35445d","font-size":"12"},value.mean.toFixed(info.digits)+" "+info.unit);
        });
        add("text",{x:(left+right)/2,y:top+rowGap*values.length+44,"text-anchor":"middle",fill:"#35445d","font-size":"13"},info.label+" ("+info.unit+")");
        chart.setAttribute("aria-label",info.label+" by scheduler for "+scenario.value+" with "+shaper.value+" shaping; bars show means and whiskers show 95 percent confidence intervals.");
        status.textContent = values[0].seeds + " fixed seeds · bars show means · whiskers show 95% confidence intervals" +
          (chosenMetric === "jain_fairness_index" ? " · fairness is shared across flows" : " · flow: "+flow.options[flow.selectedIndex].text);
      }

      [scenario,shaper,flow,metric].forEach(control => control.addEventListener("change",draw));
      fetch("/results-data",{cache:"no-store"}).then(response => response.json().then(data => {
        if (!response.ok) throw new Error(data.error || "Unable to load results.");
        rows = data.rows;
        draw();
      })).catch(error => { status.textContent = "Results unavailable: " + error.message; });
    })();
    </script>
    """


def _page(values: dict[str, str], results: list[tuple[str, dict[str, dict[str, float | int]]]] | None = None,
          error: str | None = None) -> bytes:
    title, _ = SCENARIOS[values["scenario"]]
    result_html = "" if results is None else "".join(_render_result(name, data) for name, data in results)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Adaptive QoS Simulator</title>
<style>
:root {{ color-scheme: light; --ink:#172033; --muted:#607089; --line:#dce3ed; --blue:#315fd1; --pale:#f3f6fb; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:#eef2f8; color:var(--ink); font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:36px 22px 64px; }}
.hero {{ padding:28px 30px; border-radius:18px; color:white; background:linear-gradient(120deg,#172c61,#315fd1); box-shadow:0 12px 30px #1b376329; }}
.eyebrow {{ text-transform:uppercase; letter-spacing:.12em; font-size:12px; opacity:.76; }} h1 {{ margin:7px 0 8px; font-size:clamp(27px,4vw,40px); line-height:1.1; }}
.hero p {{ max-width:800px; margin:0; color:#e3eaff; }}
.panel,.result {{ margin-top:22px; padding:24px; border:1px solid var(--line); border-radius:16px; background:#fff; box-shadow:0 5px 20px #1e31500b; }}
h2 {{ margin:0 0 16px; font-size:21px; }} .panel h2 {{ margin-bottom:18px; }}
form {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:15px; align-items:end; }}
label {{ display:grid; gap:6px; font-size:13px; font-weight:650; color:#35445d; }} select,input[type=number] {{ width:100%; min-height:42px; padding:8px 10px; border:1px solid #c8d2e1; border-radius:9px; background:#fff; color:var(--ink); font:inherit; }}
.check {{ display:flex; align-items:center; min-height:42px; gap:9px; }} .check input {{ width:17px; height:17px; accent-color:var(--blue); }}
button {{ min-height:43px; border:0; border-radius:9px; padding:10px 18px; color:#fff; background:var(--blue); font:inherit; font-weight:700; cursor:pointer; }} button:hover {{ background:#244bb2; }}
.meta {{ margin:14px 0 0; font-size:12px; color:var(--muted); }} .error {{ padding:12px 14px; border-radius:8px; color:#8b1d1d; background:#fff0ef; }}
.preset-bar {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:0 0 14px; color:#35445d; font-size:13px; }}
.preset-bar strong {{ margin-right:4px; }} button.preset-button {{ min-height:34px; padding:6px 11px; color:#31415d; background:#e8edf6; font-size:12px; }} button.preset-button:hover {{ background:#dbe3f0; }}
.result h2 {{ margin-bottom:15px; }} .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin:0 0 18px; }}
.cards div {{ padding:13px 15px; border-radius:11px; background:var(--pale); }} .cards small {{ display:block; color:var(--muted); }} .cards strong {{ display:block; margin-top:4px; font-size:22px; }} .cards em {{ font-size:13px; font-weight:500; font-style:normal; color:var(--muted); }}
.table-scroll {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; font-size:13px; white-space:nowrap; }} th,td {{ padding:10px 9px; border-bottom:1px solid var(--line); text-align:right; }} thead th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }} th:first-child,td:first-child {{ text-align:left; }} tbody th {{ font-weight:700; }}
.bar-wrap {{ display:inline-block; width:54px; height:7px; margin-right:7px; border-radius:8px; background:#e6ebf4; vertical-align:middle; overflow:hidden; }} .bar-wrap span {{ display:block; height:100%; border-radius:8px; background:var(--blue); }}
.footnote {{ margin-top:10px; color:var(--muted); font-size:12px; }} footer {{ margin-top:20px; color:var(--muted); font-size:12px; }}
.eyebrow.dark {{ color:var(--blue); opacity:1; }} .demo-heading {{ display:flex; align-items:center; justify-content:space-between; gap:16px; }}
.demo-heading h2 {{ margin:3px 0 0; }} .demo-controls {{ display:flex; gap:8px; }} button.secondary {{ color:#31415d; background:#e8edf6; }} button.secondary:hover {{ background:#dbe3f0; }}
.diagram-scroll {{ margin:14px -4px 0; overflow-x:auto; }} #traffic-diagram {{ display:block; width:100%; min-width:780px; height:auto; }}
.route {{ fill:none; stroke:#a9b7ca; stroke-width:3; stroke-dasharray:6 7; marker-end:url(#arrow); }}
.flow-node rect {{ stroke:none; }} .voice-node rect {{ fill:#fff0ed; }} .video-node rect {{ fill:#edf4ff; }} .file-node rect {{ fill:#eaf8f2; }}
.flow-node text {{ text-anchor:middle; fill:#24324a; font-size:14px; font-weight:750; }} .flow-node .sub {{ fill:#718098; font-size:10px; font-weight:500; }}
.stage>rect:first-child {{ fill:#f4f6fa; stroke:#d8e0ec; }} .stage>text {{ fill:#67768e; font-size:11px; font-weight:750; text-anchor:middle; letter-spacing:.06em; }}
.stage .slot {{ fill:#fff; stroke:#dce3ed; }} .stage .slot-text {{ fill:#485a75; font-size:11px; text-anchor:middle; }}
.shared rect {{ fill:#e9efff; stroke:#bfd0fb; stroke-width:1.5; }} .shared text {{ text-anchor:middle; fill:#253d79; font-weight:800; font-size:12px; }} .shared .shared-sub {{ fill:#66789c; font-weight:500; font-size:10px; }}
.receiver rect {{ fill:#edf8f4; stroke:#b7e4d3; }} .receiver text {{ text-anchor:middle; fill:#257756; font-size:11px; font-weight:800; }}
.packet-dot {{ filter:drop-shadow(0 1px 2px #273b5729); }} .demo-status {{ display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; padding:9px 12px; border-radius:9px; background:var(--pale); color:#475875; font-size:12px; }}
.queue-history {{ margin-top:22px; }} .queue-history h3 {{ margin:0 0 8px; font-size:17px; }} #queue-chart {{ display:block; width:100%; height:auto; }}
.explorer-controls {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:14px 0; }} .explorer-controls label {{ display:grid; gap:6px; font-size:13px; font-weight:650; color:#35445d; }} .explorer-controls select {{ width:100%; min-height:40px; padding:7px 9px; border:1px solid #c8d2e1; border-radius:9px; background:#fff; color:var(--ink); font:inherit; }} #explorer-chart {{ display:block; width:100%; height:auto; min-height:260px; }}
@media(max-width:600px) {{ main {{ padding:18px 12px 40px; }} .hero,.panel,.result {{ padding:20px 16px; }} }}
</style></head>
<body><main>
  <header class="hero"><div class="eyebrow">Honours project · network simulation</div><h1>Adaptive QoS Traffic Management</h1>
    <p>Explore how scheduling and traffic shaping affect throughput, delay, packet loss, jitter, and fairness on a shared 10 Mbps link.</p>
  </header>
  <section class="panel"><h2>Configure an experiment</h2>{error_html}{_render_form(values)}
    <p class="meta">Profile selected: {escape(title)} · queue limit: {QUEUE_LIMIT_PACKETS} waiting packets per flow · measurement window: {SIMULATION_TIME_S:g} seconds</p>
  </section>
  {_render_live_demo()}
  {_render_results_explorer()}
  {result_html}
  <p class="footnote">* Accepted packets are drained after the measurement window. Throughput counts packets completed within the window; delay includes completion after the window. Fairness uses raw per-flow throughput.</p>
  <footer>Local simulator dashboard · Python standard library · Link capacity {LINK_RATE_BPS / 1_000_000:g} Mbps</footer>
</main></body></html>"""
    return document.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    def _defaults(self) -> dict[str, str]:
        return {"scenario": "default", "scheduler": "FIFO", "shaper": "None",
                "rate": "5", "capacity": "1", "compare_all": ""}

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path == "/trace":
            self._send_trace(parse_qs(request.query))
        elif request.path == "/results-data":
            self._send_results_data()
        else:
            self._send(_page(self._defaults()))

    def _send_results_data(self) -> None:
        result_path = Path(__file__).resolve().parent / "results" / "stochastic_summary.csv"
        try:
            with result_path.open(newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))
            body = json.dumps({"rows": rows}, separators=(",", ":")).encode("utf-8")
            status = 200
        except OSError:
            body = json.dumps({"error": "Run python3 qos_simulator.py to generate stochastic result data."}).encode("utf-8")
            status = 503
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_trace(self, query: dict[str, list[str]]) -> None:
        try:
            scenario = query.get("scenario", ["default"])[0]
            scheduler = query.get("scheduler", ["FIFO"])[0]
            shaper = query.get("shaper", ["None"])[0]
            rate = float(query.get("rate", ["5"])[0])
            capacity = float(query.get("capacity", ["1"])[0])
            if scenario not in SCENARIOS or scheduler not in SCHEDULERS or shaper not in SHAPERS:
                raise ValueError("Invalid scenario, scheduler, or shaper selection.")
            if not 0.1 <= rate <= 100 or not 0.01 <= capacity <= 50:
                raise ValueError("Rate must be 0.1–100 Mbps and capacity 0.01–50 Mb per flow.")
            _, flows = SCENARIOS[scenario]
            metrics = simulate(
                flows=flows,
                scheduler_name=scheduler,
                shaper_name=shaper,
                shaper_rate_bps=int(rate * 1_000_000),
                bucket_capacity_bits=int(capacity * 1_000_000),
                include_trace=True,
                trace_packet_limit=180,
            )
            events = metrics["_summary"].get("event_trace", [])
            payload = {
                "scenario": scenario,
                "scheduler": scheduler,
                "shaper": shaper,
                "events": events,
                "queue_history": metrics["_summary"].get("queue_history", []),
                "event_count": len(events),
                "simulation_end_s": max((float(event["time_s"]) for event in events), default=0.0),
            }
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, UnicodeDecodeError) as exc:
            body = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 100_000)
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            values = self._defaults()
            for key in values:
                values[key] = form.get(key, [values[key]])[0]
            if values["scenario"] not in SCENARIOS:
                raise ValueError("Choose a listed traffic profile.")
            if values["scheduler"] not in SCHEDULERS:
                raise ValueError("Choose a listed scheduler.")
            if values["shaper"] not in SHAPERS:
                raise ValueError("Choose a listed shaper.")
            rate = float(values["rate"])
            capacity = float(values["capacity"])
            if not 0.1 <= rate <= 100 or not 0.01 <= capacity <= 50:
                raise ValueError("Rate must be 0.1–100 Mbps and capacity 0.01–50 Mb per flow.")
            _, flows = SCENARIOS[values["scenario"]]
            names = list(SCHEDULERS) if values["compare_all"] == "on" else [values["scheduler"]]
            results = [(name, simulate(
                flows=flows,
                scheduler_name=name,
                shaper_name=values["shaper"],
                shaper_rate_bps=int(rate * 1_000_000),
                bucket_capacity_bits=int(capacity * 1_000_000),
            )) for name in names]
            self._send(_page(values, results))
        except (ValueError, UnicodeDecodeError) as exc:
            values = self._defaults()
            self._send(_page(values, error=str(exc)), status=400)

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f"QoS simulator dashboard running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
