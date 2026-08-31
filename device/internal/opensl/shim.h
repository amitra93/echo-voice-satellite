/* Runtime OpenSL ES shim.  Keep the library and its interface IDs out of the
 * linker's symbol table: a device without libOpenSLES must still boot. */
#ifndef EM_OPENSL_SHIM_H
#define EM_OPENSL_SHIM_H

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <SLES/OpenSLES.h>
#include <SLES/OpenSLES_Android.h>
#include <SLES/OpenSLES_AndroidConfiguration.h>

extern void em_go_recorder_cb(long long);
extern void em_go_player_cb(long long);

typedef struct {
	void *dl;
	SLObjectItf obj;
	SLEngineItf itf;
	SLObjectItf outmix;
	SLInterfaceID iid_engine, iid_record, iid_queue, iid_config, iid_play;
} em_engine;

typedef struct {
	SLObjectItf obj;
	SLRecordItf record;
	SLAndroidSimpleBufferQueueItf queue;
	void **bufs;
	int nbuf, bufBytes;
} em_recorder;

typedef struct {
	SLObjectItf obj;
	SLPlayItf play;
	SLAndroidSimpleBufferQueueItf queue;
	void **bufs;
	int nbuf, bufBytes;
} em_player;

static char *em_dup(const char *s) {
	size_t n = strlen(s) + 1;
	char *p = (char *)malloc(n);
	if (p) memcpy(p, s, n);
	return p;
}

static char *em_result(const char *what, SLresult result) {
	char buf[160];
	snprintf(buf, sizeof(buf), "%s failed: SLresult 0x%x", what, (unsigned)result);
	return em_dup(buf);
}

static char *em_iid(void *dl, const char *name, SLInterfaceID *out) {
	void *symbol = dlsym(dl, name);
	if (!symbol) {
		const char *error = dlerror();
		char buf[160];
		snprintf(buf, sizeof(buf), "dlsym %s: %s", name, error ? error : "not found");
		return em_dup(buf);
	}
	*out = *(const SLInterfaceID *)symbol;
	return NULL;
}

static SLuint32 em_preset(int preset) {
	switch (preset) {
	case 1: return SL_ANDROID_RECORDING_PRESET_VOICE_RECOGNITION;
	case 2: return SL_ANDROID_RECORDING_PRESET_VOICE_COMMUNICATION;
	default: return SL_ANDROID_RECORDING_PRESET_GENERIC;
	}
}

typedef SLresult (*em_create_engine_fn)(SLObjectItf *, SLuint32,
	const SLEngineOption *, SLuint32, const SLInterfaceID *, const SLboolean *);

static char *em_sl_open(const char *path, em_engine *out) {
	memset(out, 0, sizeof(*out));
	void *dl = dlopen(path, RTLD_NOW | RTLD_LOCAL);
	if (!dl) {
		const char *error = dlerror();
		return em_dup(error ? error : "dlopen failed");
	}
	em_create_engine_fn create = (em_create_engine_fn)dlsym(dl, "slCreateEngine");
	if (!create) {
		char *error = em_dup(dlerror() ? dlerror() : "slCreateEngine not found");
		dlclose(dl);
		return error;
	}
	SLObjectItf obj = NULL;
	SLresult r = create(&obj, 0, NULL, 0, NULL, NULL);
	if (r != SL_RESULT_SUCCESS) { dlclose(dl); return em_result("slCreateEngine", r); }
	r = (*obj)->Realize(obj, SL_BOOLEAN_FALSE);
	if (r != SL_RESULT_SUCCESS) { (*obj)->Destroy(obj); dlclose(dl); return em_result("engine Realize", r); }
	char *error = NULL;
	if ((error = em_iid(dl, "SL_IID_ENGINE", &out->iid_engine)) ||
	    (error = em_iid(dl, "SL_IID_RECORD", &out->iid_record)) ||
	    (error = em_iid(dl, "SL_IID_ANDROIDSIMPLEBUFFERQUEUE", &out->iid_queue)) ||
	    (error = em_iid(dl, "SL_IID_ANDROIDCONFIGURATION", &out->iid_config)) ||
	    (error = em_iid(dl, "SL_IID_PLAY", &out->iid_play))) {
		(*obj)->Destroy(obj); dlclose(dl); return error;
	}
	r = (*obj)->GetInterface(obj, out->iid_engine, &out->itf);
	if (r != SL_RESULT_SUCCESS) { (*obj)->Destroy(obj); dlclose(dl); return em_result("GetInterface ENGINE", r); }
	r = (*out->itf)->CreateOutputMix(out->itf, &out->outmix, 0, NULL, NULL);
	if (r == SL_RESULT_SUCCESS) r = (*out->outmix)->Realize(out->outmix, SL_BOOLEAN_FALSE);
	if (r != SL_RESULT_SUCCESS) {
		if (out->outmix) (*out->outmix)->Destroy(out->outmix);
		(*obj)->Destroy(obj); dlclose(dl); return em_result("CreateOutputMix", r);
	}
	out->dl = dl; out->obj = obj;
	return NULL;
}

static void em_engine_close(em_engine *e) {
	if (e->outmix) (*e->outmix)->Destroy(e->outmix);
	if (e->obj) (*e->obj)->Destroy(e->obj);
	if (e->dl) dlclose(e->dl);
	memset(e, 0, sizeof(*e));
}

static void em_recorder_cb(SLAndroidSimpleBufferQueueItf q, void *ctx) {
	(void)q; em_go_recorder_cb((long long)(intptr_t)ctx);
}
static void em_player_cb(SLAndroidSimpleBufferQueueItf q, void *ctx) {
	(void)q; em_go_player_cb((long long)(intptr_t)ctx);
}

static char *em_alloc_buffers(void ***out, int count, int bytes) {
	void **bufs = (void **)calloc((size_t)count, sizeof(void *));
	if (!bufs) return em_dup("out of memory allocating buffers");
	for (int i = 0; i < count; i++) {
		bufs[i] = malloc((size_t)bytes);
		if (!bufs[i]) {
			for (int j = 0; j < i; j++) free(bufs[j]);
			free(bufs); return em_dup("out of memory allocating a buffer");
		}
	}
	*out = bufs; return NULL;
}

static char *em_recorder_open(em_engine *e, int preset, int rate, int bytes,
	int count, long long ctx, em_recorder *out) {
	memset(out, 0, sizeof(*out));
	SLDataLocator_IODevice in = {SL_DATALOCATOR_IODEVICE, SL_IODEVICE_AUDIOINPUT,
		SL_DEFAULTDEVICEID_AUDIOINPUT, NULL};
	SLDataSource source = {&in, NULL};
	SLDataLocator_AndroidSimpleBufferQueue loc = {SL_DATALOCATOR_ANDROIDSIMPLEBUFFERQUEUE, (SLuint32)count};
	SLDataFormat_PCM format = {SL_DATAFORMAT_PCM, 1, (SLuint32)rate * 1000,
		SL_PCMSAMPLEFORMAT_FIXED_16, SL_PCMSAMPLEFORMAT_FIXED_16,
		SL_SPEAKER_FRONT_CENTER, SL_BYTEORDER_LITTLEENDIAN};
	SLDataSink sink = {&loc, &format};
	const SLInterfaceID ids[] = {e->iid_queue, e->iid_config};
	const SLboolean required[] = {SL_BOOLEAN_TRUE, SL_BOOLEAN_TRUE};
	SLresult r = (*e->itf)->CreateAudioRecorder(e->itf, &out->obj, &source, &sink, 2, ids, required);
	if (r != SL_RESULT_SUCCESS) return em_result("CreateAudioRecorder", r);
	SLAndroidConfigurationItf config = NULL;
	if ((r = (*out->obj)->GetInterface(out->obj, e->iid_config, &config)) != SL_RESULT_SUCCESS) goto fail_config;
	SLuint32 value = em_preset(preset);
	if ((r = (*config)->SetConfiguration(config, SL_ANDROID_KEY_RECORDING_PRESET, &value, sizeof(value))) != SL_RESULT_SUCCESS) goto fail_config;
	if ((r = (*out->obj)->Realize(out->obj, SL_BOOLEAN_FALSE)) != SL_RESULT_SUCCESS) goto fail_config;
	if ((r = (*out->obj)->GetInterface(out->obj, e->iid_record, &out->record)) != SL_RESULT_SUCCESS) goto fail_config;
	if ((r = (*out->obj)->GetInterface(out->obj, e->iid_queue, &out->queue)) != SL_RESULT_SUCCESS) goto fail_config;
	if ((r = (*out->queue)->RegisterCallback(out->queue, em_recorder_cb, (void *)(intptr_t)ctx)) != SL_RESULT_SUCCESS) goto fail_config;
	if (em_alloc_buffers(&out->bufs, count, bytes)) { r = SL_RESULT_MEMORY_FAILURE; goto fail_config; }
	out->nbuf = count; out->bufBytes = bytes; return NULL;
fail_config:
	if (out->obj) (*out->obj)->Destroy(out->obj);
	return em_result("recorder initialization", r);
}

static char *em_recorder_prime(em_recorder *r) {
	for (int i = 0; i < r->nbuf; i++) {
		SLresult result = (*r->queue)->Enqueue(r->queue, r->bufs[i], (SLuint32)r->bufBytes);
		if (result != SL_RESULT_SUCCESS) return em_result("Enqueue recorder", result);
	}
	return NULL;
}
static char *em_recorder_start(em_recorder *r) { SLresult x = (*r->record)->SetRecordState(r->record, SL_RECORDSTATE_RECORDING); return x == SL_RESULT_SUCCESS ? NULL : em_result("start recorder", x); }
static char *em_recorder_stop(em_recorder *r) { SLresult x = (*r->record)->SetRecordState(r->record, SL_RECORDSTATE_STOPPED); return x == SL_RESULT_SUCCESS ? NULL : em_result("stop recorder", x); }
static void *em_recorder_bufptr(em_recorder *r, int i) { return r->bufs[i]; }
static char *em_recorder_enqueue(em_recorder *r, int i) { SLresult x = (*r->queue)->Enqueue(r->queue, r->bufs[i], (SLuint32)r->bufBytes); return x == SL_RESULT_SUCCESS ? NULL : em_result("Enqueue recorder", x); }
static void em_recorder_close(em_recorder *r) {
	if (!r->obj) return;
	(*r->obj)->Destroy(r->obj);
	for (int i = 0; i < r->nbuf; i++) free(r->bufs[i]);
	free(r->bufs); memset(r, 0, sizeof(*r));
}

static char *em_player_open(em_engine *e, int rate, int bytes, int count, long long ctx, em_player *out) {
	memset(out, 0, sizeof(*out));
	SLDataLocator_AndroidSimpleBufferQueue loc = {SL_DATALOCATOR_ANDROIDSIMPLEBUFFERQUEUE, (SLuint32)count};
	SLDataFormat_PCM format = {SL_DATAFORMAT_PCM, 1, (SLuint32)rate * 1000,
		SL_PCMSAMPLEFORMAT_FIXED_16, SL_PCMSAMPLEFORMAT_FIXED_16,
		SL_SPEAKER_FRONT_CENTER, SL_BYTEORDER_LITTLEENDIAN};
	SLDataSource source = {&loc, &format};
	SLDataLocator_OutputMix output = {SL_DATALOCATOR_OUTPUTMIX, e->outmix};
	SLDataSink sink = {&output, NULL};
	const SLInterfaceID ids[] = {e->iid_queue};
	const SLboolean required[] = {SL_BOOLEAN_TRUE};
	SLresult r = (*e->itf)->CreateAudioPlayer(e->itf, &out->obj, &source, &sink, 1, ids, required);
	if (r != SL_RESULT_SUCCESS) return em_result("CreateAudioPlayer", r);
	if ((r = (*out->obj)->Realize(out->obj, SL_BOOLEAN_FALSE)) != SL_RESULT_SUCCESS) goto fail_player;
	if ((r = (*out->obj)->GetInterface(out->obj, e->iid_play, &out->play)) != SL_RESULT_SUCCESS) goto fail_player;
	if ((r = (*out->obj)->GetInterface(out->obj, e->iid_queue, &out->queue)) != SL_RESULT_SUCCESS) goto fail_player;
	if ((r = (*out->queue)->RegisterCallback(out->queue, em_player_cb, (void *)(intptr_t)ctx)) != SL_RESULT_SUCCESS) goto fail_player;
	if (em_alloc_buffers(&out->bufs, count, bytes)) { r = SL_RESULT_MEMORY_FAILURE; goto fail_player; }
	out->nbuf = count; out->bufBytes = bytes;
	if ((r = (*out->play)->SetPlayState(out->play, SL_PLAYSTATE_PLAYING)) != SL_RESULT_SUCCESS) goto fail_player;
	return NULL;
fail_player:
	if (out->obj) (*out->obj)->Destroy(out->obj);
	for (int i = 0; i < out->nbuf; i++) free(out->bufs[i]);
	free(out->bufs);
	return em_result("player initialization", r);
}
static void *em_player_bufptr(em_player *p, int i) { return p->bufs[i]; }
static char *em_player_enqueue(em_player *p, int i, int bytes) { SLresult x = (*p->queue)->Enqueue(p->queue, p->bufs[i], (SLuint32)bytes); return x == SL_RESULT_SUCCESS ? NULL : em_result("Enqueue player", x); }
static char *em_player_clear(em_player *p) { SLresult x = (*p->queue)->Clear(p->queue); return x == SL_RESULT_SUCCESS ? NULL : em_result("Clear player", x); }
static char *em_player_stop(em_player *p) { SLresult x = (*p->play)->SetPlayState(p->play, SL_PLAYSTATE_STOPPED); return x == SL_RESULT_SUCCESS ? NULL : em_result("stop player", x); }
static void em_player_close(em_player *p) {
	if (!p->obj) return;
	(*p->play)->SetPlayState(p->play, SL_PLAYSTATE_STOPPED);
	(*p->obj)->Destroy(p->obj);
	for (int i = 0; i < p->nbuf; i++) free(p->bufs[i]);
	free(p->bufs); memset(p, 0, sizeof(*p));
}

#endif
