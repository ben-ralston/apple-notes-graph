import { describe, it, expect, vi } from 'vitest';
import { watchUpdateStatus } from './updateStatus.js';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('watchUpdateStatus', () => {
  it('reports the status straight away', async () => {
    const onChange = vi.fn();
    const api = { getUiUpdateStatus: async () => ({ stale: true }) };
    watchUpdateStatus({ api, target: new EventTarget(), onChange });
    await tick();
    expect(onChange).toHaveBeenLastCalledWith(true);
  });

  it('checks again when the window regains focus', async () => {
    const onChange = vi.fn();
    const getUiUpdateStatus = vi
      .fn()
      .mockResolvedValueOnce({ stale: false })
      .mockResolvedValueOnce({ stale: true });
    const target = new EventTarget();
    watchUpdateStatus({ api: { getUiUpdateStatus }, target, onChange });
    await tick();
    expect(onChange).toHaveBeenLastCalledWith(false);
    target.dispatchEvent(new Event('focus'));
    await tick();
    expect(onChange).toHaveBeenLastCalledWith(true);
  });

  it('shows nothing when the check fails or the API is missing', async () => {
    const failing = vi.fn();
    watchUpdateStatus({
      api: { getUiUpdateStatus: async () => { throw new Error('ipc'); } },
      target: new EventTarget(),
      onChange: failing,
    });
    await tick();
    expect(failing).toHaveBeenLastCalledWith(false);

    const missing = vi.fn();
    watchUpdateStatus({ api: undefined, target: new EventTarget(), onChange: missing });
    await tick();
    expect(missing).toHaveBeenLastCalledWith(false);
  });

  it('only treats an explicit true as stale', async () => {
    const onChange = vi.fn();
    watchUpdateStatus({
      api: { getUiUpdateStatus: async () => ({ stale: 'yes' }) },
      target: new EventTarget(),
      onChange,
    });
    await tick();
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it('stops listening once unsubscribed', async () => {
    const getUiUpdateStatus = vi.fn(async () => ({ stale: false }));
    const target = new EventTarget();
    const stop = watchUpdateStatus({ api: { getUiUpdateStatus }, target, onChange: vi.fn() });
    await tick();
    stop();
    target.dispatchEvent(new Event('focus'));
    await tick();
    expect(getUiUpdateStatus).toHaveBeenCalledOnce();
  });
});
