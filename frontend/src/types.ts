export interface Health {
  status: string;
  state: "idle" | "loading" | "ready" | "degraded";
  market_data_loaded: boolean;
  models_loaded: boolean;
  errors: string[];
  data_as_of: string | null;
  last_refresh: RefreshResult | null;
  timestamp: string;
  calendar?: { covered_through: string | null; days_left: number | null; low: boolean };
}

export interface EvidenceView {
  n: number;
  verdict: { code: string; text: string };
  wins?: number;
  win_rate_pct?: number;
  total_net?: number;
  fees_total?: number;
  avg_per_trade?: number;
  avg_win?: number | null;
  avg_loss?: number | null;
  best_trade?: number;
  worst_trade?: number;
  profit_factor?: number | null;
  per_lot?: { mean: number; stdev: number | null; ci95: [number, number] | null };
  trades_needed_for_precision?: { target_rs_per_lot: number; trades: number | null };
  by_strategy: { strategy: string; n: number; net: number }[];
  by_exit_reason: { reason: string; n: number }[];
  references: { name: string; rs_per_lot: number; weight: string; note: string }[];
  caveats: string[];
}

export interface RefreshResult {
  at: string;
  ok: boolean;
  trigger: string;
  latest_date: string;
  rows: number;
  changed: boolean;
  message: string;
}

export interface Journal {
  journal_id: string;
  label: string;
  initial_capital: number;
  strategy_track: string;
  status: string;
  notes: string;
  created_at: string;
  open_trades: number;
  closed_trades: number;
  total_pnl: number;
  account_equity: number;
  win_rate_pct: number | null;
  avg_trade_pnl: number | null;
  account_dd_pct: number;
}

export interface AccountStatus {
  journal_id: string;
  label: string;
  account_equity: number;
  initial_capital: number;
  cumulative_pnl: number;
  account_dd_pct: number;
  open_trades: number;
  closed_trades: number;
  win_rate_pct: number | null;
  available_capital: number;
  reserve_capital: number;
  vix_now: number | null;
  spot_now: number | null;
  regime: string | null;
  market_date: string | null;
  data_state: string;
  last_refresh: RefreshResult | null;
}

export interface Track {
  should_enter: boolean;
  quality_score: number | null;
  signal: string | null;
  recommended_strategy: string | null;
  suggested_lots: number | null;
  available_capital: number | null;
  capital_to_deploy: number | null;
  dte_window: string | null;
  skip_reason: string | null;
  eligible_strategies: string[];
}

export interface Signal {
  timestamp: string;
  within_entry_window: boolean;
  spot: number;
  vix: number;
  regime: string;
  weekly: Track;
  monthly: Track;
  macro_context: string[];
  reasoning: string[];
}

export interface Leg {
  action: "BUY" | "SELL";
  strike: number;
  option_type: "CE" | "PE";
  entry_price: number;
}

export interface Trade {
  journal_id: string;
  trade_id: string;
  strategy: string;
  entry_date: string;
  entry_time_ist: string;
  expiry_date: string;
  legs: Leg[];
  lots: number;
  lot_size: number;
  entry_credit: number;
  entry_vix: number | null;
  entry_spot: number | null;
  regime: string;
  capital_deployed: number;
  notes: string;
  status: "open" | "closed";
  exit_date: string | null;
  exit_debit: number | null;
  exit_reason: string | null;
  brokerage: number | null;
  gross_pnl: number | null;
  net_pnl: number | null;
  days_held: number | null;
}

export interface Recommendation {
  trade_id: string;
  journal_id?: string;
  action: string;
  confidence?: number;
  current_pnl_pct?: number;
  pnl_rupees?: number;
  days_in_trade?: number;
  dte_remaining?: number;
  risk_score?: number;
  reasoning?: string[];
}

export interface DailyEntry {
  journal_id: string;
  log_date: string;
  account_equity: number;
  cumulative_pnl: number;
  open_trades: number;
  vix_close: number | null;
  regime: string;
  account_dd_pct: number;
  notes: string;
}

export interface LoginResult {
  at: string;
  ok: boolean;
  message: string;
  automatic: boolean;
}

export interface BrokerStatus {
  broker: "kite" | "none";
  configured: boolean;
  connected: boolean;
  token_expires_at: string | null;
  auto_login_enabled: boolean;
  last_login: LoginResult | null;
}

export type CredentialField = "user_id" | "password" | "api_key" | "api_secret" | "totp_secret";

/** Write-only: the server returns flags and a masked user id, never the values. */
export interface CredentialsView {
  encryption_available: boolean;
  key_source: string;
  saved: Record<CredentialField, boolean>;
  user_id_masked: string;
  auto_login_enabled: boolean;
  updated_at: string | null;
  error: string | null;
  last_login: LoginResult | null;
  auto_login_attempts_today: number;
  auto_login_blocked: boolean;
}

export interface LiveLeg {
  action: "BUY" | "SELL";
  strike: number;
  option_type: "CE" | "PE";
  entry_price: number;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
}

export interface LiveAlert {
  code: string;
  message: string;
}

export interface LiveTrade {
  trade_id: string;
  journal_id: string;
  strategy: string;
  ok: boolean;
  reason?: string;
  legs: LiveLeg[];
  mark_debit?: number;
  exit_debit_est?: number;
  unrealized_mark?: number;
  unrealized_exit?: number;
  pct_credit_captured?: number | null;
  alerts: LiveAlert[];
}

export interface LiveTrades {
  ok: boolean;
  reason?: string;
  source?: string;
  as_of?: string;
  spot?: number | null;
  trades: LiveTrade[];
}

export interface AutoStatus {
  enabled: boolean;
  paused_reason: string;
  lots: number;
  lot_size: number;
  journal_id: string;
  journal: Journal | null;
  open_trades: { trade_id: string; strategy: string; expiry_date: string; entry_date: string; entry_credit: number }[];
  consecutive_errors: number;
  last_tick: string | null;
  data_as_of: string | null;
  schedule: Record<string, string | number>;
  paper_only: boolean;
}

export interface AutoEvent {
  id: number;
  at: string;
  day: string;
  kind: "ENTRY" | "EXIT" | "SKIP" | "ERROR" | "INFO" | "PAUSE";
  code: string;
  message: string;
  trade_id: string | null;
  data: Record<string, unknown> | null;
}

export interface FillLeg {
  action: "BUY" | "SELL";
  strike: number;
  option_type: "CE" | "PE";
  entry_price: number;
  entry_bid: number | null;
  entry_ask: number | null;
  entry_ltp: number | null;
  entry_spread_cost: number | null;
  exit_price: number | null;
  exit_bid: number | null;
  exit_ask: number | null;
  leg_pnl: number | null;
}

export interface TradeFills {
  trade_id: string;
  status: "open" | "closed";
  strategy: string;
  lots: number;
  lot_size: number;
  quantity: number;
  expiry_date: string;
  entry: { date: string; time_ist: string; spot: number | null; vix: number | null; net_credit: number };
  exit: { date: string; time_ist: string | null; reason: string | null; net_debit: number | null; per_leg_recorded: boolean } | null;
  legs: FillLeg[];
  money: { gross_pnl: number | null; fees: number | null; net_pnl: number | null; credit_rupees: number; debit_rupees: number | null };
  source: { entry: string | null; exit: string | null };
}

export interface ExitPreviewTrade {
  trade_id: string;
  strategy?: string;
  expiry_date?: string;
  dte?: number;
  days_held?: number;
  decision: "EXIT" | "HOLD" | "SETTLE" | "UNKNOWN";
  reason: string;
  message?: string;
  entry_credit?: number;
  exit_debit_est?: number;
  pct_credit_captured?: number | null;
  gross_pnl_est?: number;
  fees_est?: number;
  net_pnl_est?: number;
  legs?: { action: string; strike: number; option_type: string; exit_price: number; bid: number; ask: number }[];
}

export interface ExitPreview {
  ok: boolean;
  reason?: string;
  in_session: boolean;
  auto_enabled: boolean;
  as_of: string;
  spot?: number;
  vix?: number;
  trades: ExitPreviewTrade[];
}

export interface AlertStatus {
  configured: boolean;
  channel: string;
  last_ok: boolean | null;
  last_error: string | null;
  last_sent_at: number | null;
  saved: { bot_token: boolean; chat_id: boolean };
  chat_id_masked: string;
  source: "dashboard" | "environment" | null;
  encryption_available: boolean;
}

export interface BackupStatus {
  offsite_configured: boolean;
  last_offsite_at: string | null;
  last_local_at: string | null;
  last_error: string | null;
  local_copies: number;
  stale: boolean;
}

export interface AutoPreview {
  decision: "ENTER" | "SKIP";
  code: string;
  message: string;
  transient?: boolean;
  blocks: { code: string; message: string }[];
  strategy?: string;
  expiry?: string;
  dte?: number;
  lots?: number;
  net_credit?: number;
  legs?: { action: string; strike: number; option_type: string; entry_price: number; bid: number; ask: number }[];
  inputs?: { spot?: number; vix?: number; ret_5d?: number | null; data_as_of?: string };
}

export interface Live {
  ok: boolean;
  source: string | null;
  reason?: string;
  spot?: number;
  vix?: number;
  as_of?: string;
}
