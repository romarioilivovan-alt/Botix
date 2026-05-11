# Botix Core (Rust)

High-performance core components for Botix trading bot, written in Rust for minimal latency.

## Architecture

The Rust core is designed to handle the most latency-sensitive parts of the trading system:

### Phase 1: WebSocket Consumers (Priority)
- **MEXC WebSocket client**: Subscribe to depth updates for 0-fee symbols
- **Binance WebSocket client**: Subscribe to depth updates for reference pricing
- **Output**: Stream parsed depth updates to Python via Unix domain socket (or named pipe on Windows)
- **Goal**: Sub-millisecond parsing with simd-json, zero-copy where possible

### Phase 2: Aggregator & Stats (Medium Priority)
- **Order book aggregation**: Maintain top-N levels for each symbol
- **Rolling statistics**: Welford algorithm for O(1) mean/σ computation
- **Fair value calculation**: Cross-venue spread, z-score, velocity metrics
- **Output**: Computed stats back to Python for strategy evaluation

### Phase 3: Execution Engine (Future)
- **Order management**: Submit, cancel, track fills
- **Position tracking**: Real-time P&L, stop-loss ladder
- **Risk checks**: Pre-trade validation, kill switch
- **Goal**: End-to-end latency under 5ms from signal to order submission

## Current Status

**Phase 0 (Skeleton)**: Basic project structure only. No functional code yet.

## Development Plan

1. **Phase 1a**: MEXC WebSocket consumer with depth parsing
2. **Phase 1b**: Binance WebSocket consumer
3. **Phase 1c**: IPC bridge to Python (Unix socket / named pipe)
4. **Phase 2a**: Order book aggregation in Rust
5. **Phase 2b**: Rolling stats (Welford, velocity, OFI)
6. **Phase 3**: Full execution engine (if Phase 1-2 show significant latency improvement)

## Why Rust?

- **Zero-cost abstractions**: No GC pauses, predictable performance
- **Memory safety**: No segfaults, data races caught at compile time
- **Ecosystem**: tokio for async, simd-json for fast parsing, dashmap for concurrent data structures
- **Interop**: Easy to call from Python via ctypes/cffi or Unix sockets

## Building

```bash
cd rust_core
cargo build --release
```

**Note**: Rust toolchain (rustc 1.70+) required. Install from https://rustup.rs/

## Integration with Python

The Python bot will communicate with Rust core via:
- **Input**: Strategy signals, configuration updates
- **Output**: Market data stream, computed statistics

Communication protocol TBD (likely MessagePack over Unix socket for low overhead).

## Performance Targets

- **WebSocket parsing**: < 100μs per depth update
- **Stats computation**: < 50μs per symbol
- **End-to-end (Phase 3)**: Signal → order submission in < 5ms

## License

Same as parent project.
