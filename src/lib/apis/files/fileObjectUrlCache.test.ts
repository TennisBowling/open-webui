import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getFileObjectUrlById, primeFileObjectUrlById, revokeFileObjectUrlById } from './index';

describe('local file object URL cache priming', () => {
	const fileId = 'fresh-upload';

	beforeEach(() => {
		vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:local-upload');
		vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
		vi.stubGlobal('fetch', vi.fn());
	});

	afterEach(() => {
		revokeFileObjectUrlById(fileId);
		vi.restoreAllMocks();
		vi.unstubAllGlobals();
	});

	it('reuses local uploaded bytes without fetching the server thumbnail', async () => {
		const file = new File(['image bytes'], 'photo.webp', { type: 'image/webp' });

		expect(primeFileObjectUrlById(fileId, file, 768)).toBe('blob:local-upload');
		await expect(getFileObjectUrlById('token', fileId, undefined, undefined, 768)).resolves.toBe(
			'blob:local-upload'
		);
		expect(fetch).not.toHaveBeenCalled();
	});

	it('does not allocate another object URL when the same cache key is primed twice', () => {
		const file = new File(['image bytes'], 'photo.webp', { type: 'image/webp' });

		primeFileObjectUrlById(fileId, file, 1024);
		primeFileObjectUrlById(fileId, file, 1024);

		expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
	});
});
