use clap::Parser;

fn main() -> anyhow::Result<()> {
    symbol_linguist::loc::run(symbol_linguist::loc::LocArgs::parse())
}
