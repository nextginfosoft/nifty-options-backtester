export interface Health {
  status: string;
  state: "idle" | "loading" | "ready" | "degraded";
  market_data_loaded: boolean;
  models_loaded: boolean;
  errors: string[];
  timestamp: string;
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

export interface BrokerStatus {
  broker: "kite" | "none";
  configured: boolean;
  connected: boolean;
  token_expires_at: string | null;
}

export interface Live {
  ok: boolean;
  source: string | null;
  reason?: string;
  spot?: number;
  vix?: number;
  as_of?: string;
}
