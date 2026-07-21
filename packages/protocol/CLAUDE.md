# notehook-protocol

Shared pydantic models mirroring the reverse-engineered Supernote API spec
(`docs/openapi/components/schemas/*.yaml`), plus login-hash helpers. Both server and
client depend on this package so the two sides can't drift.

## Rules

- Field names must match the spec **exactly**, including camelCase
  (`equipmentNo`, `lastUpdateTime`) and snake_case (`content_hash`,
  `path_display`) inconsistencies. Never "clean them up".
- All models extend `ProtocolModel` (`models/common.py`): extras are ignored
  because real firmware may send undocumented fields.
- Be lenient on types the spec is inconsistent about — e.g. `size` accepts
  str or int via `_LenientSize` (`models/file.py`). Add similar coercions when
  device captures reveal new mismatches, don't tighten existing ones.
- `crypto.py`: both documented login hash schemes exist
  (`sha256(md5(pw)+rc)` and `md5(md5(pw)+rc)`) because which one firmware uses
  is unknown. Keep both until a real-device capture settles it.
- No I/O, no HTTP, no server/client imports here — pure models and hashing only.
