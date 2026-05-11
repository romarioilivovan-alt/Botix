mod aggregator;
mod allocator;
mod config;
mod engine;
mod executor;
mod http;
mod mexc;
mod opportunity;
mod state;
mod store;
mod universe;
mod ws_clients;

use anyhow::{Context, Result};
use clap::Parser;
use config::{default_config_path, default_root, load_config};
use std::env;
use std::net::SocketAddr;
use std::path::PathBuf;
use store::Store;
use tracing_subscriber::EnvFilter;

#[derive(Debug, Parser)]
#[command(name = "rust-bot", about = "Rust rewrite of the MEXC 0-fee flipper")]
struct Args {
    #[arg(long, env = "ZFEE_ROOT")]
    root: Option<PathBuf>,

    #[arg(long, env = "ZFEE_CONFIG_PATH")]
    config: Option<PathBuf>,

    #[arg(long, env = "ZFEE_DB_PATH")]
    db: Option<PathBuf>,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info,rust_bot=debug")),
        )
        .init();

    let args = Args::parse();
    let root = args.root.unwrap_or(default_root()?);
    let config_path = args.config.unwrap_or_else(|| default_config_path(&root));
    let db_path = args
        .db
        .or_else(|| env::var_os("ZFEE_DB_PATH").map(PathBuf::from))
        .unwrap_or_else(|| root.join("data.sqlite"));
    let cache_path = env::var_os("ZFEE_UNIVERSE_CACHE")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join(".universe_cache.rust.json"));

    let cfg = load_config(&config_path)?;
    let store = Store::open(db_path).await?;
    let engine = engine::Engine::new(cfg.clone(), config_path, cache_path, store).await?;
    engine.start().await?;

    let frontend_dir = root.join("frontend");
    let app = http::router(engine.clone(), frontend_dir);
    let addr: SocketAddr = format!("{}:{}", cfg.host, cfg.port)
        .parse()
        .with_context(|| format!("invalid bind address {}:{}", cfg.host, cfg.port))?;
    tracing::info!("Rust bot listening on http://{}", addr);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let _ = tokio::signal::ctrl_c().await;
            engine.shutdown().await;
        })
        .await?;
    Ok(())
}
