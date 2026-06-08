import { describe, it, expect } from 'vitest';
import { isSecretName, valueLooksCredentialed, shouldRedact } from '../../src/lib/secret_guard';

// PARITY with agent/secret_guard.py — these cases mirror the Python heuristic.
// If the Python SECRET_NAME_PATTERN / _CREDENTIALED_URL change, change both.
describe('secret_guard parity — isSecretName', () => {
  for (const name of [
    'API_TOKEN', 'DB_PASSWORD', 'SIGNING_KEY', 'CLIENT_SECRET', 'DATABASE_URL',
    'SERVICE_URI', 'DB_CONNECTION', 'CONNSTR', 'JWT_AUDIENCE', 'OAUTH_SCOPE',
    'private_key', 'x_auth_header', 'BEARER_PREFIX', 'PWD_SALT', 'DSN',
  ]) {
    it(`flags ${name}`, () => expect(isSecretName(name)).toBe(true));
  }
  for (const name of ['LOG_LEVEL', 'TIMEOUT_MS', 'FEATURE_FLAG_X', 'REGION', 'ENDPOINT', 'MAX_RETRIES']) {
    it(`passes ${name}`, () => expect(isSecretName(name)).toBe(false));
  }
});

describe('secret_guard parity — valueLooksCredentialed', () => {
  it('flags scheme://user:pass@host', () =>
    expect(valueLooksCredentialed('postgres://u:p4ss@db.internal/prod')).toBe(true));
  it('flags https with embedded auth', () =>
    expect(valueLooksCredentialed('https://admin:hunter2@svc/api')).toBe(true));
  it('passes a plain URL with no userinfo', () =>
    expect(valueLooksCredentialed('https://example.com/path')).toBe(false));
  it('passes a non-URL value', () => expect(valueLooksCredentialed('debug')).toBe(false));
  it('passes null/undefined/empty', () => {
    expect(valueLooksCredentialed(null)).toBe(false);
    expect(valueLooksCredentialed(undefined)).toBe(false);
    expect(valueLooksCredentialed('')).toBe(false);
  });
});

describe('secret_guard parity — shouldRedact (name OR value)', () => {
  it('redacts a secret-named var regardless of value', () =>
    expect(shouldRedact('API_TOKEN', 'anything')).toBe(true));
  it('redacts a non-secret name with a credentialed value', () =>
    expect(shouldRedact('ENDPOINT', 'https://a:b@h/x')).toBe(true));
  it('does not redact a plain non-secret var', () =>
    expect(shouldRedact('LOG_LEVEL', 'debug')).toBe(false));
  it('secret name still redacts when value is null', () =>
    expect(shouldRedact('SECRET_KEY', null)).toBe(true));
});
