#!/usr/bin/env python3
"""Multi-week comprehensive backtest with detailed statistical analysis."""

from datetime import datetime, timedelta, date
import statistics

import pandas as pd
from rich.console import Console
from rich.table import Table

from config import Config
from research.connection import ResearchConnection
from research.query import query_minute_bars
from strategy.backtest.engine import BacktestEngine

console = Console()

def get_trading_days(start: date, end: date) -> list[date]:
    """Get all weekdays between start and end (trading days only)."""
    days = []
    current = start
    while current <= end:
        # Only include weekdays (0=Monday, 6=Sunday)
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def run_backtest_multi_day(start_date: date, end_date: date, symbols: list[str] | None = None):
    """Run backtest across multiple days and aggregate results."""

    config = Config()
    con = ResearchConnection()

    if symbols is None:
        symbols = ["SNDL", "MARA", "RIOT", "BBAI", "SOUN", "PLUG"]

    console.print(f"\n[bold blue]Comprehensive Backtest: {start_date} to {end_date}[/bold blue]")
    console.print(f"[dim]Testing {len(symbols)} symbols across trading days[/dim]\n")

    trading_days = get_trading_days(start_date, end_date)
    console.print(f"[cyan]{len(trading_days)} trading days in range[/cyan]\n")

    # Aggregate results
    all_trades: list[dict] = []
    daily_results: dict[date, dict] = {}
    symbol_stats: dict[str, dict] = {}

    engine = BacktestEngine(config)

    # Progress bar
    processed = 0
    for trading_day in trading_days:
        # Query minute bars for all symbols that day
        day_symbols_with_data = []
        day_bars = {}

        for symbol in symbols:
            try:
                bars = query_minute_bars(con, symbol, trading_day)
                if len(bars) > 0:
                    day_symbols_with_data.append(symbol)
                    day_bars[symbol] = bars
            except Exception as e:
                pass  # No data for this day

        if not day_symbols_with_data:
            processed += 1
            continue

        # Initialize daily results if needed
        if trading_day not in daily_results:
            daily_results[trading_day] = {"trades": [], "pnl": 0.0}

        # Run backtest for each symbol with data
        for symbol in day_symbols_with_data:
            bars = day_bars[symbol]

            try:
                result = engine.run(bars, symbol)

                # Aggregate
                daily_results[trading_day]["pnl"] += result.total_pnl

                # Initialize symbol stats if needed
                if symbol not in symbol_stats:
                    symbol_stats[symbol] = {"trades": [], "wins": 0, "losses": 0, "pnl": 0.0}

                for trade in result.trades:
                    trade_data = {
                        "date": trading_day.isoformat(),
                        "symbol": symbol,
                        "entry_time": trade.entry_time,
                        "entry_price": trade.entry_price,
                        "stop_price": trade.stop_price,
                        "target_price": trade.target_price,
                        "quantity": trade.quantity,
                        "exit_price": trade.exit_price,
                        "exit_reason": trade.exit_reason,
                        "pnl": trade.realized_pnl or 0.0,
                        "r_multiple": trade.r_multiple or 0.0,
                    }
                    all_trades.append(trade_data)
                    daily_results[trading_day]["trades"].append(trade_data)

                    # Symbol stats
                    symbol_stats[symbol]["trades"].append(trade_data)
                    symbol_stats[symbol]["pnl"] += trade.realized_pnl or 0.0
                    if (trade.realized_pnl or 0) > 0:
                        symbol_stats[symbol]["wins"] += 1
                    else:
                        symbol_stats[symbol]["losses"] += 1
            except Exception as e:
                console.print(f"[red]Error backtesting {symbol} on {trading_day}: {e}[/red]")

        processed += 1
        console.print(f"[dim]Processed {processed}/{len(trading_days)} days[/dim]", end="\r")

    console.print()

    # === SUMMARY STATISTICS ===
    if not all_trades:
        console.print("[yellow]No trades found in backtest range[/yellow]")
        return

    # Daily summary
    console.print("\n[bold cyan]=== DAILY SUMMARY ===[/bold cyan]")
    daily_table = Table(title="P&L by Day")
    daily_table.add_column("Date", style="cyan")
    daily_table.add_column("Trades", justify="right")
    daily_table.add_column("P&L", justify="right")

    daily_pnls = []
    for day in sorted(daily_results.keys()):
        daily_pnl = daily_results[day]["pnl"]
        daily_pnls.append(daily_pnl)
        color = "green" if daily_pnl > 0 else "red"
        daily_table.add_row(
            str(day),
            str(len(daily_results[day]["trades"])),
            f"[{color}]${daily_pnl:,.2f}[/{color}]"
        )
    console.print(daily_table)

    # Symbol summary
    console.print("\n[bold cyan]=== SYMBOL SUMMARY ===[/bold cyan]")
    symbol_table = Table(title="Results by Symbol")
    symbol_table.add_column("Symbol", style="cyan")
    symbol_table.add_column("Trades", justify="right")
    symbol_table.add_column("Wins", justify="right")
    symbol_table.add_column("Win %", justify="right")
    symbol_table.add_column("P&L", justify="right")
    symbol_table.add_column("Avg R", justify="right")

    for symbol in sorted(symbol_stats.keys()):
        stats = symbol_stats[symbol]
        trades = stats["trades"]
        if not trades:
            continue

        win_pct = (stats["wins"] / len(trades)) * 100 if trades else 0
        avg_r = statistics.mean([t["r_multiple"] for t in trades]) if trades else 0

        color = "green" if stats["pnl"] > 0 else "red"
        symbol_table.add_row(
            symbol,
            str(len(trades)),
            str(stats["wins"]),
            f"{win_pct:.1f}%",
            f"[{color}]${stats['pnl']:,.2f}[/{color}]",
            f"{avg_r:.2f}R"
        )
    console.print(symbol_table)

    # Overall stats
    total_trades = len(all_trades)
    total_pnl = sum(t["pnl"] for t in all_trades)
    wins = len([t for t in all_trades if t["pnl"] > 0])
    losses = len([t for t in all_trades if t["pnl"] < 0])
    win_rate = (wins / total_trades) * 100 if total_trades else 0

    r_multiples = [t["r_multiple"] for t in all_trades]
    avg_r = statistics.mean(r_multiples) if r_multiples else 0

    # Separate wins and losses for analysis
    win_trades = [t for t in all_trades if t["pnl"] > 0]
    loss_trades = [t for t in all_trades if t["pnl"] < 0]

    avg_win = statistics.mean([t["pnl"] for t in win_trades]) if win_trades else 0
    avg_loss = statistics.mean([t["pnl"] for t in loss_trades]) if loss_trades else 0
    max_loss = min([t["pnl"] for t in loss_trades]) if loss_trades else 0

    profit_factor = (sum([t["pnl"] for t in win_trades]) / abs(sum([t["pnl"] for t in loss_trades]))) if loss_trades and sum([t["pnl"] for t in loss_trades]) < 0 else 0

    console.print("\n[bold cyan]=== OVERALL STATISTICS ===[/bold cyan]")
    console.print(f"Total Trades: {total_trades}")
    console.print(f"Wins: {wins} | Losses: {losses}")
    console.print(f"Win Rate: {win_rate:.1f}%")
    console.print(f"Total P&L: [{'green' if total_pnl > 0 else 'red'}]${total_pnl:,.2f}[/{'green' if total_pnl > 0 else 'red'}]")
    console.print(f"Avg Win: ${avg_win:,.2f} | Avg Loss: ${avg_loss:,.2f}")
    console.print(f"Max Loss: ${max_loss:,.2f}")
    if profit_factor > 0:
        console.print(f"Profit Factor: {profit_factor:.2f}x")
    console.print(f"Avg R-Multiple: {avg_r:.2f}R")

    # === LOSS ANALYSIS ===
    if loss_trades:
        console.print("\n[bold yellow]=== LOSS ANALYSIS ===[/bold yellow]")
        console.print(f"Total Losing Trades: {len(loss_trades)}")

        loss_table = Table(title="Top 10 Losses (for pattern analysis)")
        loss_table.add_column("Date", style="cyan")
        loss_table.add_column("Symbol", style="cyan")
        loss_table.add_column("Entry", justify="right")
        loss_table.add_column("Stop", justify="right")
        loss_table.add_column("Exit", justify="right")
        loss_table.add_column("P&L", justify="right")
        loss_table.add_column("Reason")

        sorted_losses = sorted(loss_trades, key=lambda t: t["pnl"])
        for trade in sorted_losses[:10]:
            loss_table.add_row(
                trade["date"],
                trade["symbol"],
                f"${trade['entry_price']:.2f}",
                f"${trade['stop_price']:.2f}",
                f"${trade['exit_price']:.2f}" if trade["exit_price"] else "-",
                f"[red]${trade['pnl']:,.2f}[/red]",
                trade["exit_reason"] or "?"
            )
        console.print(loss_table)

        # Loss metrics
        stop_hits = len([t for t in loss_trades if "stop" in (t["exit_reason"] or "").lower()])
        console.print(f"\n[dim]Losses by reason:[/dim]")
        console.print(f"  Hit stop: {stop_hits}/{len(loss_trades)}")

        # Gap-through analysis
        for trade in sorted_losses[:5]:
            entry = trade["entry_price"]
            stop = trade["stop_price"]
            exit_price = trade["exit_price"]
            if exit_price and exit_price < stop:
                gap_pct = ((stop - exit_price) / stop) * 100
                console.print(f"  [yellow]{trade['symbol']} {trade['date']}: gap-through {gap_pct:.2f}% ({stop:.2f} → {exit_price:.2f})[/yellow]")

    # === WIN ANALYSIS ===
    if win_trades:
        console.print("\n[bold green]=== WIN ANALYSIS ===[/bold green]")
        console.print(f"Total Winning Trades: {len(win_trades)}")

        win_table = Table(title="Recent Wins")
        win_table.add_column("Date", style="cyan")
        win_table.add_column("Symbol", style="cyan")
        win_table.add_column("Entry", justify="right")
        win_table.add_column("Target", justify="right")
        win_table.add_column("Exit", justify="right")
        win_table.add_column("P&L", justify="right")
        win_table.add_column("R")

        sorted_wins = sorted(win_trades, key=lambda t: t["pnl"], reverse=True)
        for trade in sorted_wins[:5]:
            win_table.add_row(
                trade["date"],
                trade["symbol"],
                f"${trade['entry_price']:.2f}",
                f"${trade['target_price']:.2f}",
                f"${trade['exit_price']:.2f}" if trade["exit_price"] else "-",
                f"[green]${trade['pnl']:,.2f}[/green]",
                f"{trade['r_multiple']:.2f}R"
            )
        console.print(win_table)

    # === RISK ASSESSMENT ===
    console.print("\n[bold yellow]=== RISK ASSESSMENT ===[/bold yellow]")

    if avg_loss != 0:
        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)
        console.print(f"Expected value per trade: ${expectancy:,.2f}")

        if profit_factor > 0:
            if profit_factor < 1.5:
                console.print("[yellow]⚠️  Profit factor < 1.5 indicates marginal edge or slippage issues[/yellow]")
            else:
                console.print("[green]✓ Profit factor suggests adequate win/loss ratio[/green]")

    # Check for consistent losses
    max_win = max([t["pnl"] for t in win_trades]) if win_trades else 0
    if max_loss < 0 and abs(max_loss) > max_win * 1.5:
        console.print(f"[red]⚠️  Max loss (${max_loss:,.2f}) >> max win (${max_win:,.2f}) — stop is too wide or slippage is severe[/red]")

    # Sharpe-ish ratio (simplified)
    if len(daily_pnls) > 1:
        daily_std = statistics.stdev(daily_pnls) if len(daily_pnls) > 1 else 0
        if daily_std > 0:
            daily_mean = statistics.mean(daily_pnls)
            sharpe_approx = daily_mean / daily_std if daily_std > 0 else 0
            console.print(f"Daily Sharpe (approx): {sharpe_approx:.2f}")

    console.print("\n[dim]Recommendations:[/dim]")
    if win_rate < 40:
        console.print("[red]• Win rate < 40% — strategy may lack edge even with profitable trades[/red]")
    if profit_factor < 1.3 and profit_factor > 0:
        console.print("[yellow]• Profit factor < 1.3 — consider tightening stops or improving entry[/yellow]")
    if len(loss_trades) > 0 and max_loss < avg_loss / 2:
        console.print("[yellow]• Losses are inconsistent — gap-throughs may be a factor[/yellow]")

    console.print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Multi-week comprehensive backtest")
    parser.add_argument("--start", type=str, default="2026-06-05", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-06-12", help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", type=str, default="SNDL,MARA,RIOT,BBAI,SOUN,PLUG", help="Comma-separated symbols")

    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    symbols = args.symbols.split(",")

    run_backtest_multi_day(start, end, symbols)
