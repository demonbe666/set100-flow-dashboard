(() => {
  const section = document.querySelector('.paper-section');
  if (!section || !document.body.classList.contains('view-paper')) return;

  const selectedPortfolioId = section.dataset.portfolioId || 'avg30-top5flow';
  let portfolioConfigs = [];
  try {
    portfolioConfigs = JSON.parse(section.dataset.portfoliosJson || '[]');
  } catch (error) {
    portfolioConfigs = [];
  }
  portfolioConfigs = portfolioConfigs.map((item) => ({
    ...item,
    topN: Number(item.topN),
    initialCapital: Number(item.initialCapital),
    positionBudget: Number(item.positionBudget),
    maxDailyEntries: Number(item.maxDailyEntries),
    commissionRate: Number(item.commissionRate),
    avgLowDays: Number(item.avgLowDays || 30),
    bidField: item.bidField || 'bid_price',
    slField: item.slField || 'sl_price',
  }));
  if (!portfolioConfigs.length) return;
  const selectedConfig = (
    portfolioConfigs.find((item) => item.id === selectedPortfolioId) || portfolioConfigs[0]
  );
  let pollTimer = null;
  let pollBusy = false;
  let latestRows = [];
  const stateCache = new Map();
  const revisionCache = new Map();
  let cloudSyncAvailable = false;
  let cloudSyncPersistent = false;
  let cloudDirty = false;

  function defaultState(config = selectedConfig) {
    return {
      version: 1,
      startDate: config.startDate,
      initialCapital: config.initialCapital,
      cash: config.initialCapital,
      realizedPnl: 0,
      positions: {},
      orders: [],
      equityHistory: [{
        ts: `${config.startDate}T00:00:00+07:00`,
        equity: config.initialCapital,
      }],
      dailyEntries: {},
      lastProcessedBars: {},
      lastScanTs: null,
    };
  }

  function normalizeState(state, config = selectedConfig) {
    if (!state || state.version !== 1 || state.startDate !== config.startDate) {
      return defaultState(config);
    }
    state.positions = state.positions || {};
    state.orders = state.orders || [];
    state.equityHistory = state.equityHistory || [];
    state.dailyEntries = state.dailyEntries || {};
    state.lastProcessedBars = state.lastProcessedBars || {};
    return state;
  }

  function loadLocalState(config = selectedConfig) {
    try {
      const state = JSON.parse(localStorage.getItem(config.storageKey) || 'null');
      return normalizeState(state, config);
    } catch (error) {
      return defaultState(config);
    }
  }

  function loadState(config = selectedConfig) {
    return stateCache.get(config.id) || loadLocalState(config);
  }

  function saveLocalState(state, config = selectedConfig, markDirty = true) {
    state.orders = state.orders.slice(-1000);
    state.equityHistory = state.equityHistory.slice(-2500);
    state.localUpdatedAt = new Date().toISOString();
    stateCache.set(config.id, state);
    localStorage.setItem(config.storageKey, JSON.stringify(state));
    if (markDirty) cloudDirty = true;
  }

  function stateHasActivity(state) {
    return Boolean(
      state?.orders?.length || Object.keys(state?.positions || {}).length ||
      Object.values(state?.dailyEntries || {}).some((items) => items?.length)
    );
  }

  function stateActivityScore(state) {
    if (!state) return 0;
    const dailyEntries = Object.values(state.dailyEntries || {}).reduce(
      (sum, items) => sum + (Array.isArray(items) ? items.length : 0),
      0,
    );
    return (
      (state.orders || []).length * 1000000 +
      Object.keys(state.positions || {}).length * 10000 +
      dailyEntries * 100 +
      Math.max(0, (state.equityHistory || []).length - 1)
    );
  }

  function stateLatestTimestamp(state) {
    const timestamps = [
      state?.lastScanTs,
      state?.localUpdatedAt,
      ...(state?.orders || []).map((order) => order.marketTs),
      ...(state?.equityHistory || []).map((point) => point.ts),
    ].filter(Boolean).map((value) => Date.parse(value)).filter(Number.isFinite);
    return timestamps.length ? Math.max(...timestamps) : 0;
  }

  function tradeLevels(row, config = selectedConfig) {
    const hasBidField = row && Object.prototype.hasOwnProperty.call(row, config.bidField);
    const hasSlField = row && Object.prototype.hasOwnProperty.call(row, config.slField);
    return {
      bid: Number(hasBidField ? row[config.bidField] : (row?.bid_price ?? 0)),
      tp: Number(row?.tp_price ?? 0),
      sl: Number(hasSlField ? row[config.slField] : (row?.sl_price ?? 0)),
    };
  }

  function useCloudPayload(payload, config) {
    const state = normalizeState(payload.state, config);
    stateCache.set(config.id, state);
    revisionCache.set(config.id, Number(payload.revision || 0));
    saveLocalState(state, config, false);
    cloudSyncAvailable = true;
    cloudSyncPersistent = Boolean(payload.persistent);
    cloudDirty = false;
    return state;
  }

  async function pushCloudState(state, config = selectedConfig, migration = false, throwOnError = false) {
    saveLocalState(state, config, false);
    try {
      const response = await fetch(`/api/paper_state/${encodeURIComponent(config.id)}`, {
        method: 'PUT',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          state,
          baseRevision: revisionCache.get(config.id) || null,
          migration,
        }),
      });
      const payload = await response.json();
      if (response.status === 409 && payload.state) {
        return useCloudPayload(payload, config);
      }
      if (!response.ok || payload.error) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      return useCloudPayload(payload, config);
    } catch (error) {
      cloudSyncAvailable = false;
      stateCache.set(config.id, state);
      if (throwOnError) throw error;
      return state;
    }
  }

  async function syncCloudState(config = selectedConfig) {
    const localState = normalizeState(loadState(config), config);
    const response = await fetch(
      `/api/paper_state/${encodeURIComponent(config.id)}?t=${Date.now()}`,
      { cache: 'no-store' },
    );
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    cloudSyncAvailable = true;
    cloudSyncPersistent = Boolean(payload.persistent);
    if (!payload.exists) {
      revisionCache.set(config.id, 0);
      return stateHasActivity(localState)
        ? pushCloudState(localState, config, true, true)
        : localState;
    }

    const cloudState = normalizeState(payload.state, config);
    const localScore = stateActivityScore(localState);
    const cloudScore = stateActivityScore(cloudState);
    const localTs = stateLatestTimestamp(localState);
    const cloudTs = Math.max(stateLatestTimestamp(cloudState), Date.parse(payload.updated_at || '') || 0);
    revisionCache.set(config.id, Number(payload.revision || 0));
    if (cloudScore > localScore || (cloudScore === localScore && cloudTs > localTs)) {
      return useCloudPayload(payload, config);
    }
    return stateHasActivity(localState)
      ? pushCloudState(localState, config, true, true)
      : useCloudPayload(payload, config);
  }

  async function loadCloudState(config = selectedConfig, allowMigration = true) {
    const localState = loadLocalState(config);
    try {
      const response = await fetch(
        `/api/paper_state/${encodeURIComponent(config.id)}?t=${Date.now()}`,
        { cache: 'no-store' },
      );
      const payload = await response.json();
      if (!response.ok || payload.error) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      cloudSyncAvailable = true;
      cloudSyncPersistent = Boolean(payload.persistent);
      if (!payload.exists) {
        stateCache.set(config.id, localState);
        revisionCache.set(config.id, 0);
        return stateHasActivity(localState) && allowMigration
          ? pushCloudState(localState, config, true)
          : localState;
      }

      const cloudState = normalizeState(payload.state, config);
      stateCache.set(config.id, cloudState);
      revisionCache.set(config.id, Number(payload.revision || 0));
      if (allowMigration && stateHasActivity(localState) && !stateHasActivity(cloudState)) {
        return pushCloudState(localState, config, true);
      }
      saveLocalState(cloudState, config, false);
      return cloudState;
    } catch (error) {
      cloudSyncAvailable = false;
      stateCache.set(config.id, localState);
      return localState;
    }
  }

  function marketParts(timestamp) {
    const match = String(timestamp || '').match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/);
    if (!match) return null;
    return {
      date: match[1],
      hour: Number(match[2]),
      minute: Number(match[3]),
      minutes: Number(match[2]) * 60 + Number(match[3]),
    };
  }

  function sessionAt(minutes) {
    if (minutes >= 9 * 60 + 55 && minutes <= 12 * 60 + 25) return 'morning';
    if (minutes >= 14 * 60 && minutes <= 16 * 60 + 25) return 'afternoon';
    return null;
  }

  function isEntryWindow(minutes) {
    return (
      (minutes >= 10 * 60 + 15 && minutes <= 11 * 60 + 45) ||
      (minutes >= 14 * 60 && minutes <= 15 * 60 + 45)
    );
  }

  function sessionEnd(session) {
    return session === 'morning' ? 12 * 60 + 25 : 16 * 60 + 25;
  }

  function money(value) {
    return Number(value || 0).toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function price(value) {
    return Number(value || 0).toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  function addOrder(state, order) {
    state.orders.push({
      id: `${order.marketTs}-${order.side}-${order.ticker}-${state.orders.length + 1}`,
      ...order,
    });
  }

  function closePosition(state, ticker, exitPrice, marketTs, reason, config) {
    const position = state.positions[ticker];
    if (!position) return false;
    const grossValue = position.qty * exitPrice;
    const sellCommission = grossValue * config.commissionRate;
    const pnl = grossValue - sellCommission - position.entryValue - position.buyCommission;
    state.cash += grossValue - sellCommission;
    state.realizedPnl += pnl;
    addOrder(state, {
      marketTs,
      side: 'SELL',
      ticker,
      qty: position.qty,
      price: exitPrice,
      grossValue,
      commission: sellCommission,
      realizedPnl: pnl,
      reason,
    });
    delete state.positions[ticker];
    return true;
  }

  function calculateMetrics(state, rows = latestRows, config = selectedConfig) {
    const rowMap = new Map(rows.map((row) => [row.ticker, row]));
    let marketValue = 0;
    let unrealized = 0;
    Object.values(state.positions).forEach((position) => {
      const row = rowMap.get(position.ticker);
      const last = Number(row?.last_price ?? position.lastPrice ?? position.entryPrice);
      position.lastPrice = last;
      const value = position.qty * last;
      const estimatedSellCommission = value * config.commissionRate;
      marketValue += value;
      unrealized += value - estimatedSellCommission - position.entryValue - position.buyCommission;
    });
    const equity = state.cash + marketValue - marketValue * config.commissionRate;
    return { marketValue, unrealized, equity };
  }

  function calculateTradeStats(state, tradeDate = null) {
    const closedTrades = (state.orders || []).filter((order) => (
      order.side === 'SELL' && order.realizedPnl != null
      && (!tradeDate || String(order.marketTs || '').slice(0, 10) === tradeDate)
    ));
    const winPnls = closedTrades
      .map((order) => Number(order.realizedPnl))
      .filter((pnl) => pnl > 0);
    const lossPnls = closedTrades
      .map((order) => Number(order.realizedPnl))
      .filter((pnl) => pnl < 0);
    const grossProfit = winPnls.reduce((sum, pnl) => sum + pnl, 0);
    const grossLoss = Math.abs(lossPnls.reduce((sum, pnl) => sum + pnl, 0));
    const avgWin = winPnls.length ? grossProfit / winPnls.length : null;
    const avgLoss = lossPnls.length ? grossLoss / lossPnls.length : null;
    const wins = winPnls.length;
    const total = closedTrades.length;
    return {
      wins,
      total,
      winRate: total ? wins / total * 100 : null,
      riskReward: avgWin && avgLoss ? avgWin / avgLoss : null,
      profitFactor: grossLoss > 0 ? grossProfit / grossLoss : (grossProfit > 0 ? Infinity : null),
    };
  }

  function calculateDailyWinStats(state) {
    const pnlByDate = new Map();
    (state.orders || []).forEach((order) => {
      if (order.side !== 'SELL' || order.realizedPnl == null || !order.marketTs) return;
      const tradeDate = String(order.marketTs).slice(0, 10);
      pnlByDate.set(tradeDate, (pnlByDate.get(tradeDate) || 0) + Number(order.realizedPnl));
    });
    const dailyPnls = [...pnlByDate.values()].filter((pnl) => pnl !== 0);
    const wins = dailyPnls.filter((pnl) => pnl > 0).length;
    const total = dailyPnls.length;
    return {
      wins,
      total,
      winRate: total ? wins / total * 100 : null,
    };
  }

  function recordEquity(state, marketTs, metrics) {
    if (!marketTs) return;
    const point = {
      ts: marketTs,
      equity: metrics.equity,
      cash: state.cash,
      realizedPnl: state.realizedPnl,
    };
    const history = state.equityHistory;
    if (history.length && history[history.length - 1].ts === marketTs) {
      history[history.length - 1] = point;
    } else {
      history.push(point);
    }
  }

  async function processPortfolioScan(data, config) {
    const state = loadState(config);
    const rows = Array.isArray(data.rows) ? data.rows : [];
    latestRows = rows;
    const usableRows = rows.filter((row) => row.market_ts && row.session_date);
    const latestMarketTs = usableRows.reduce(
      (latest, row) => (!latest || row.market_ts > latest ? row.market_ts : latest),
      null,
    );
    const latest = marketParts(latestMarketTs);
    const rowMap = new Map(rows.map((row) => [row.ticker, row]));
    const orderCountBefore = state.orders.length;

    Object.values({ ...state.positions }).forEach((position) => {
      const row = rowMap.get(position.ticker);
      if (!row?.market_ts) return;
      if (row.session_date !== position.date) {
        closePosition(
          state,
          position.ticker,
          Number(position.lastPrice || position.entryPrice),
          `${position.date}T16:25:00+07:00`,
          'SESSION_RECOVERY',
          config,
        );
        return;
      }
      if (row.market_ts <= (position.lastCheckedBar || position.entryMarketTs)) return;
      position.lastPrice = Number(row.last_price || position.entryPrice);
      const parts = marketParts(row.market_ts);
      if (parts && parts.minutes >= sessionEnd(position.session)) {
        const sessionExit = position.session === 'morning'
          ? row.morning_exit_price
          : row.afternoon_exit_price;
        closePosition(
          state,
          position.ticker,
          Number(sessionExit || row.last_price),
          row.market_ts,
          'SESSION_CLOSE',
          config,
        );
        return;
      }
      if (row.market_ts > position.entryMarketTs) {
        const hitStop = Number(row.latest_bar_low) <= position.sl;
        const hitTarget = Number(row.latest_bar_high) >= position.tp;
        if (hitStop) {
          const barOpen = Number(row.latest_bar_open || position.sl);
          const stopFill = Math.min(barOpen, position.sl);
          closePosition(
            state,
            position.ticker,
            stopFill,
            row.market_ts,
            hitTarget ? 'SL_AND_TP_SL_FIRST' : (stopFill < position.sl ? 'SL_GAP' : 'SL'),
            config,
          );
          return;
        }
        if (hitTarget) {
          closePosition(state, position.ticker, position.tp, row.market_ts, 'TP', config);
          return;
        }
      }
      position.lastCheckedBar = row.market_ts;
    });

    const canTrade = (
      data.session_is_today && latest && latest.date >= config.startDate &&
      isEntryWindow(latest.minutes)
    );
    if (canTrade) {
      const entriesToday = state.dailyEntries[latest.date] || [];
      const queue = rows
        .filter((row) => {
          const levels = tradeLevels(row, config);
          return (
            row.session_date === latest.date && Number(row.flow_rank) >= 1 &&
            Number(row.flow_rank) <= config.topN && Number(row.prev_net_flow) > 0 &&
            levels.bid > 0 && levels.tp > levels.bid && levels.sl > 0
          );
        })
        .sort((a, b) => Number(a.flow_rank) - Number(b.flow_rank));

      for (const row of queue) {
        if (entriesToday.length >= config.maxDailyEntries) break;
        if (state.positions[row.ticker] || entriesToday.includes(row.ticker)) continue;
        if (!row.market_ts || row.market_ts <= (state.lastProcessedBars[row.ticker] || '')) continue;
        const levels = tradeLevels(row, config);
        const bid = levels.bid;
        const barLow = Number(row.latest_bar_low);
        const barHigh = Number(row.latest_bar_high);
        if (!(barLow <= bid && barHigh >= bid)) continue;
        const open = Number(row.latest_bar_open || bid);
        const fillPrice = open <= bid ? Math.min(open, bid) : bid;
        const grossBudget = Math.min(config.positionBudget, state.cash) / (1 + config.commissionRate);
        const qty = Math.floor(grossBudget / fillPrice / 100) * 100;
        if (qty <= 0) continue;
        const entryValue = qty * fillPrice;
        const buyCommission = entryValue * config.commissionRate;
        if (entryValue + buyCommission > state.cash) continue;
        state.cash -= entryValue + buyCommission;
        state.positions[row.ticker] = {
          ticker: row.ticker,
          date: latest.date,
          session: sessionAt(latest.minutes),
          qty,
          entryPrice: fillPrice,
          entryValue,
          buyCommission,
          tp: levels.tp,
          sl: levels.sl,
          flowRank: Number(row.flow_rank),
          entryMarketTs: row.market_ts,
          lastCheckedBar: row.market_ts,
          lastPrice: Number(row.last_price || fillPrice),
        };
        entriesToday.push(row.ticker);
        state.dailyEntries[latest.date] = entriesToday;
        addOrder(state, {
          marketTs: row.market_ts,
          side: 'BUY',
          ticker: row.ticker,
          qty,
          price: fillPrice,
          grossValue: entryValue,
          commission: buyCommission,
          realizedPnl: null,
          reason: `AVG${config.avgLowDays}_TOP${config.topN}_BID_FILL_R${row.flow_rank}`,
        });
      }
    }

    const hasStarted = !latest || latest.date >= config.startDate;
    if (hasStarted) {
      rows.forEach((row) => {
        if (row.market_ts) state.lastProcessedBars[row.ticker] = row.market_ts;
      });
      state.lastScanTs = latestMarketTs || data.ts || new Date().toISOString();
    }
    const metrics = calculateMetrics(state, rows, config);
    if (hasStarted) recordEquity(state, state.lastScanTs, metrics);
    saveLocalState(state, config);
    return {
      state,
      rows,
      orderAdded: state.orders.length > orderCountBefore,
    };
  }

  async function processScan(data) {
    let selectedResult = null;
    let orderAdded = false;
    for (const config of portfolioConfigs) {
      const result = await processPortfolioScan(data, config);
      orderAdded = orderAdded || result.orderAdded;
      if (config.id === selectedConfig.id) selectedResult = result;
    }
    selectedResult = selectedResult || {
      state: loadState(selectedConfig),
      rows: Array.isArray(data.rows) ? data.rows : [],
    };
    render(selectedResult.state, selectedResult.rows, data, selectedConfig);
    if (orderAdded && typeof window.playAlertSound === 'function') {
      window.playAlertSound();
    }
  }

  function render(
    state = loadState(selectedConfig),
    rows = latestRows,
    data = null,
    config = selectedConfig,
  ) {
    const metrics = calculateMetrics(state, rows, config);
    const totalReturn = (metrics.equity / state.initialCapital - 1) * 100;
    const tradeStats = calculateTradeStats(state);
    const dailyWinStats = calculateDailyWinStats(state);
    const values = {
      paperEquity: money(metrics.equity),
      paperCash: money(state.cash),
      paperMarketValue: money(metrics.marketValue),
      paperRealized: money(state.realizedPnl),
      paperUnrealized: money(metrics.unrealized),
      paperReturn: `${totalReturn >= 0 ? '+' : ''}${totalReturn.toFixed(2)}%`,
      paperWinRate: tradeStats.total ? `${tradeStats.winRate.toFixed(1)}% (${tradeStats.wins}/${tradeStats.total})` : '-',
      paperDailyWinRate: dailyWinStats.total ? `${dailyWinStats.winRate.toFixed(1)}% (${dailyWinStats.wins}/${dailyWinStats.total}D)` : '-',
      paperRiskReward: tradeStats.riskReward ? `${tradeStats.riskReward.toFixed(2)}R` : '-',
      paperProfitFactor: tradeStats.profitFactor === Infinity ? 'Inf' : (
        tradeStats.profitFactor ? tradeStats.profitFactor.toFixed(2) : '-'
      ),
    };
    Object.entries(values).forEach(([id, value]) => {
      const element = document.getElementById(id);
      if (element) element.textContent = value;
    });
    setMetricColor('paperRealized', state.realizedPnl);
    setMetricColor('paperUnrealized', metrics.unrealized);
    setMetricColor('paperReturn', totalReturn);

    const rowMap = new Map(rows.map((row) => [row.ticker, row]));
    const positions = Object.values(state.positions);
    const positionBody = document.querySelector('#paperPositionsTable tbody');
    positionBody.innerHTML = positions.length ? positions.map((position) => {
      const last = Number(rowMap.get(position.ticker)?.last_price ?? position.lastPrice);
      const marketValue = position.qty * last;
      const unrealized = (
        marketValue - marketValue * config.commissionRate -
        position.entryValue - position.buyCommission
      );
      return `<tr>
        <td class="ticker">${escapeHtml(position.ticker)}</td><td>${position.qty.toLocaleString()}</td>
        <td>${price(position.entryPrice)}</td><td>${price(last)}</td>
        <td class="pos">${price(position.tp)}</td><td class="neg">${price(position.sl)}</td>
        <td>${position.flowRank}</td><td>${money(marketValue)}</td>
        <td class="${unrealized >= 0 ? 'pos' : 'neg'}">${unrealized >= 0 ? '+' : ''}${money(unrealized)}</td>
        <td>${escapeHtml(position.session)}</td></tr>`;
    }).join('') : '<tr class="paper-empty"><td colspan="10">No open positions</td></tr>';

    const orderBody = document.querySelector('#paperOrdersTable tbody');
    const orders = [...state.orders].reverse();
    orderBody.innerHTML = orders.length ? orders.slice(0, 200).map((order) => `<tr>
      <td>${escapeHtml(String(order.marketTs).replace('T', ' ').slice(0, 16))}</td>
      <td class="${order.side === 'BUY' ? 'paper-side-buy' : 'paper-side-sell'}">${order.side}</td>
      <td class="ticker">${escapeHtml(order.ticker)}</td><td>${Number(order.qty).toLocaleString()}</td>
      <td>${price(order.price)}</td><td>${money(order.grossValue)}</td><td>${money(order.commission)}</td>
      <td class="${Number(order.realizedPnl || 0) >= 0 ? 'pos' : 'neg'}">${order.realizedPnl == null ? '-' : `${order.realizedPnl >= 0 ? '+' : ''}${money(order.realizedPnl)}`}</td>
      <td>${escapeHtml(order.reason)}</td></tr>`).join('') : '<tr class="paper-empty"><td colspan="9">No orders</td></tr>';

    const queueBody = document.querySelector('#paperQueueTable tbody');
    const queue = rows
      .filter((row) => Number(row.flow_rank) >= 1 && Number(row.flow_rank) <= config.topN)
      .sort((a, b) => Number(a.flow_rank) - Number(b.flow_rank));
    queueBody.innerHTML = queue.length ? queue.map((row) => {
      const levels = tradeLevels(row, config);
      return `<tr>
        <td>${row.flow_rank}</td><td class="ticker">${escapeHtml(row.ticker)}</td>
        <td>${price(row.last_price)}</td><td class="pos">${price(levels.bid)}</td>
        <td>${price(levels.tp)}</td><td class="neg">${price(levels.sl)}</td></tr>`;
    }).join('') : '<tr class="paper-empty"><td colspan="6">Waiting for market data</td></tr>';
    drawChart(state.equityHistory, config);

    if (data) updateStatus(state, positions, data, config);
  }

  function setMetricColor(id, value) {
    const element = document.getElementById(id);
    if (!element) return;
    element.classList.toggle('pos', Number(value) >= 0);
    element.classList.toggle('neg', Number(value) < 0);
  }

  function updateStatus(state, positions, data, config = selectedConfig) {
    const status = document.getElementById('paperStatus');
    const detail = document.getElementById('paperStatusDetail');
    const dot = document.getElementById('paperStatusDot');
    const parts = marketParts(state.lastScanTs);
    const active = data.session_is_today && parts && parts.date >= config.startDate;
    status.textContent = active ? (isEntryWindow(parts.minutes) ? 'AUTO ACTIVE' : 'MONITORING') : 'WAITING';
    const syncLabel = cloudSyncAvailable
      ? (cloudDirty ? 'LOCAL CHANGES' : (cloudSyncPersistent ? 'CLOUD SAVED' : 'SERVER TEMP'))
      : 'THIS DEVICE ONLY';
    detail.textContent = active
      ? `Market ${String(state.lastScanTs).replace('T', ' ').slice(0, 16)} | ${positions.length} open | ${(state.dailyEntries[parts.date] || []).length}/${config.maxDailyEntries} entries | ${syncLabel}`
      : `Start ${config.startDate} | latest session ${data.session_label || '-'} | ${syncLabel}`;
    dot.classList.toggle('active', Boolean(active));
    dot.classList.remove('error');
  }

  function drawChart(history, config = selectedConfig) {
    const canvas = document.getElementById('paperEquityChart');
    if (!canvas) return;
    const width = Math.max(320, canvas.clientWidth);
    const height = 260;
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext('2d');
    context.scale(scale, scale);
    context.fillStyle = '#11151a';
    context.fillRect(0, 0, width, height);
    const points = history.length ? history : [{ equity: config.initialCapital }];
    const values = points.map((point) => Number(point.equity));
    let minValue = Math.min(...values, config.initialCapital);
    let maxValue = Math.max(...values, config.initialCapital);
    const padding = Math.max((maxValue - minValue) * 0.12, config.initialCapital * 0.002);
    minValue -= padding;
    maxValue += padding;
    const left = 74;
    const right = 16;
    const top = 18;
    const bottom = 28;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    context.strokeStyle = '#232932';
    context.fillStyle = '#6b7785';
    context.font = '10px Consolas, monospace';
    for (let index = 0; index <= 4; index += 1) {
      const y = top + plotHeight * index / 4;
      const value = maxValue - (maxValue - minValue) * index / 4;
      context.beginPath();
      context.moveTo(left, y);
      context.lineTo(width - right, y);
      context.stroke();
      context.fillText(Math.round(value).toLocaleString('en-US'), 4, y + 3);
    }
    context.strokeStyle = '#2ecf85';
    context.lineWidth = 2;
    context.beginPath();
    points.forEach((point, index) => {
      const x = left + (points.length === 1 ? 0 : plotWidth * index / (points.length - 1));
      const y = top + (maxValue - Number(point.equity)) / (maxValue - minValue) * plotHeight;
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.stroke();
    context.fillStyle = '#6b7785';
    context.fillText(String(points[0]?.ts || '').slice(5, 10), left, height - 9);
    context.textAlign = 'right';
    context.fillText(
      String(points[points.length - 1]?.ts || '').slice(5, 16).replace('T', ' '),
      width - right,
      height - 9,
    );
    context.textAlign = 'left';
  }

  async function poll(manual = false) {
    if (pollBusy) return;
    pollBusy = true;
    const status = document.getElementById('paperStatus');
    const detail = document.getElementById('paperStatusDetail');
    const dot = document.getElementById('paperStatusDot');
    status.textContent = 'REFRESHING';
    detail.textContent = 'Loading SET100 market snapshot...';
    try {
      const response = await fetch(`/api/paper_scan?t=${Date.now()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.error) throw new Error(data.error);
      await processScan(data);
    } catch (error) {
      status.textContent = 'DATA ERROR';
      detail.textContent = error.message;
      dot.classList.remove('active');
      dot.classList.add('error');
    } finally {
      pollBusy = false;
      clearTimeout(pollTimer);
      pollTimer = setTimeout(() => poll(false), manual ? 5000 : 60000);
    }
  }

  function exportOrders() {
    const state = loadState(selectedConfig);
    const headers = ['market_time', 'side', 'ticker', 'qty', 'price', 'gross_value', 'commission', 'realized_pnl', 'reason'];
    const escapeCsv = (value) => `"${String(value == null ? '' : value).replaceAll('"', '""')}"`;
    const rows = state.orders.map((order) => [
      order.marketTs, order.side, order.ticker, order.qty, order.price,
      order.grossValue, order.commission, order.realizedPnl, order.reason,
    ].map(escapeCsv).join(','));
    const blob = new Blob([[headers.join(','), ...rows].join('\n')], {
      type: 'text/csv;charset=utf-8',
    });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `paper-trade-${selectedConfig.id}-orders-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  async function syncOnline() {
    if (pollBusy) return;
    pollBusy = true;
    const status = document.getElementById('paperStatus');
    const detail = document.getElementById('paperStatusDetail');
    const dot = document.getElementById('paperStatusDot');
    status.textContent = 'CLOUD SYNC';
    detail.textContent = 'Syncing all paper portfolios...';
    dot.classList.remove('error');
    try {
      for (const config of portfolioConfigs) {
        await syncCloudState(config);
      }
      render(loadState(selectedConfig), latestRows, null, selectedConfig);
      status.textContent = 'SYNCED';
      detail.textContent = cloudSyncPersistent
        ? 'Online portfolio saved. Auto scan stays local until you press cloud sync again.'
        : 'Server sync completed, but storage is temporary. Auto scan stays local.';
      cloudDirty = false;
      dot.classList.add('active');
    } catch (error) {
      cloudSyncAvailable = false;
      status.textContent = 'SYNC ERROR';
      detail.textContent = error.message;
      dot.classList.remove('active');
      dot.classList.add('error');
    } finally {
      pollBusy = false;
    }
  }

  document.getElementById('paperRefreshBtn')?.addEventListener('click', () => poll(true));
  document.getElementById('paperSyncBtn')?.addEventListener('click', syncOnline);
  document.getElementById('paperExportBtn')?.addEventListener('click', exportOrders);
  document.getElementById('paperResetBtn')?.addEventListener('click', async () => {
    if (!window.confirm(`Reset ${selectedConfig.label} to 5,000,000 baht?`)) return;
    try {
      const response = await fetch(
        `/api/paper_state/${encodeURIComponent(selectedConfig.id)}`,
        { method: 'DELETE', cache: 'no-store' },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      localStorage.removeItem(selectedConfig.storageKey);
      revisionCache.set(selectedConfig.id, 0);
      const state = defaultState(selectedConfig);
      stateCache.set(selectedConfig.id, state);
      latestRows = [];
      render(state, [], null, selectedConfig);
    } catch (error) {
      window.alert(`Reset failed: ${error.message}`);
    }
  });
  window.addEventListener('resize', () => (
    drawChart(loadState(selectedConfig).equityHistory, selectedConfig)
  ));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) poll(true);
  });
  async function initialize() {
    const status = document.getElementById('paperStatus');
    const detail = document.getElementById('paperStatusDetail');
    status.textContent = 'LOCAL READY';
    detail.textContent = 'Paper portfolios are saved on this device. Press cloud sync when needed.';
    portfolioConfigs.forEach((config) => stateCache.set(config.id, loadLocalState(config)));
    render(loadState(selectedConfig), [], null, selectedConfig);
    poll(false);
  }

  initialize();
})();
