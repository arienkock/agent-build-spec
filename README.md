# Agent Build Spec

`agent-build-spec` is a standard aimed at structuring the task implementation workflow for coding agents. It defines a consistent filesystem layout and process to ensure that agents perform tasks reliably, handle verifications and rollbacks, and manage state in a predictable way. 

The `agent-build` tool provides a command-line interface that allows you to initialize, run, and manage tasks structured around this specification.

Learn more about the standard at: https://github.com/arienkock/agent-build-spec/blob/master/agent-build-spec.md

## Installation

You can install `agent-build` directly from GitHub using pip:

```bash
pip install git+https://github.com/arienkock/agent-build-spec.git
```

This will install the `agent-build` CLI command into your environment.

## Usage

For available commands and usage information, run:

```bash
agent-build --help
```