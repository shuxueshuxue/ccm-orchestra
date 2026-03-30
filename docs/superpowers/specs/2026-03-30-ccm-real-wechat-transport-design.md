# CCM Real WeChat Transport Design

## Goal

Add a real WeChat transport to `ccm-orchestra` that is independent from leon/mycel runtime services. `ccm` should be able to request a real QR code, wait for phone scan confirmation, keep a single global WeChat connection alive, poll for incoming WeChat messages, and route those messages into the existing visible-tab collaboration layer.

## Non-Goal

- No leon backend dependency
- No leon auth dependency
- No per-user or per-tab WeChat accounts in v1
- No broad transport abstraction for multiple providers yet

## Reference Boundary

`leon` is only a protocol/process reference. Its existing WeChat implementation proves the iLink API flow and data model:

- get QR code
- poll QR status
- persist bot token
- long-poll updates
- reuse context token for replies

`ccm` should copy that protocol shape, not reuse leon runtime or account model.

## Architecture

`ccm` will own one global WeChat transport state file under its own home directory. That state stores:

- bot token
- base URL
- account id
- user id
- saved timestamp
- update sync buffer
- known context tokens keyed by WeChat user id
- one bound delivery target

The bound delivery target is the default recipient for incoming phone WeChat messages. In v1 the default target will be a registered visible-tab alias from the existing peer layer. This keeps the delivery path simple and observable.

## Layers

There are now two different things called "wechat" inside `ccm`:

1. Real phone WeChat transport
2. Wechat-style peer handoff layer

They must stay separate in docs and help text.

The real transport is responsible for:

- QR connect
- status
- disconnect
- polling updates
- sending replies to phone users
- binding incoming delivery to a peer alias

The peer layer is still responsible for:

- registering visible peers
- sending messages between peers
- shifting work between peers

## V1 Commands

- `ccm wechat-connect`
- `ccm wechat-status`
- `ccm wechat-disconnect`
- `ccm wechat-bind <alias>`
- `ccm wechat-unbind`
- `ccm wechat-users`
- `ccm wechat-reply <user_id> "..."`
- `ccm wechat-poll-once`
- `ccm wechat-watch`

Existing peer commands stay:

- `ccm wechat-register`
- `ccm wechat-contacts`
- `ccm wechat-send`
- `ccm wechat-shift`

## Delivery Format

Incoming phone WeChat messages delivered to a bound visible tab should include a system-style reminder with:

- source user id
- source text
- current bound alias
- exact reply command
- exact handoff command

The receiving agent should be able to answer by copy-pasting the shown `ccm wechat-reply ...` command.

## Error Handling

- Fail loudly if no global WeChat connection exists
- Fail loudly if a reply is attempted before that user has a context token
- Fail loudly if a bound alias is missing or no longer visible
- Treat long-poll timeouts as normal `wait`, not fatal errors

## Testing

- unit tests for QR connect flow
- unit tests for transport state persistence
- unit tests for bind/unbind
- unit tests for inbound delivery envelope
- unit tests for reply command behavior
- one live smoke test for QR generation and state persistence
