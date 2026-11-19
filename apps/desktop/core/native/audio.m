// Native macOS 13 audio. No Core Audio process-tap symbols (14.2+) are linked.
#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <AudioToolbox/AudioToolbox.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreGraphics/CoreGraphics.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#include <math.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

typedef void (*SamplesFn)(void *, bool, const float *, size_t, uint32_t, uint16_t);
typedef void (*ErrorFn)(void *, const char *);
static const int64_t StartWait = 30 * NSEC_PER_SEC;
static const int64_t StopWait = 5 * NSEC_PER_SEC;

static void Failure(char **out, NSString *message) {
    if (out) *out = strdup((message ?: @"Mac audio operation failed.").UTF8String);
}
static NSString *Status(NSString *operation, OSStatus status) {
    return [NSString stringWithFormat:@"%@ (Core Audio error %d).", operation, (int)status];
}
static NSString *DeviceString(AudioDeviceID device, AudioObjectPropertySelector selector) {
    AudioObjectPropertyAddress address = { selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    CFStringRef value = NULL;
    UInt32 size = sizeof(value);
    if (AudioObjectGetPropertyData(device, &address, 0, NULL, &size, &value) != noErr || !value) return nil;
    return CFBridgingRelease(value);
}
static AudioDeviceID DefaultDevice(bool output) {
    AudioObjectPropertyAddress address = { output ? kAudioHardwarePropertyDefaultOutputDevice : kAudioHardwarePropertyDefaultInputDevice,
        kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    AudioDeviceID device = kAudioObjectUnknown;
    UInt32 size = sizeof(device);
    AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, 0, NULL, &size, &device);
    return device;
}
static NSArray<NSNumber *> *Devices(bool output) {
    AudioObjectPropertyAddress address = { kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &address, 0, NULL, &size) != noErr || size == 0) return @[];
    NSMutableData *data = [NSMutableData dataWithLength:size];
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, 0, NULL, &size, data.mutableBytes) != noErr) return @[];
    AudioDeviceID *devices = data.mutableBytes;
    NSMutableArray<NSNumber *> *result = [NSMutableArray array];
    for (UInt32 i = 0; i < size / sizeof(AudioDeviceID); i++) {
        AudioObjectPropertyAddress streams = { kAudioDevicePropertyStreams,
            output ? kAudioDevicePropertyScopeOutput : kAudioDevicePropertyScopeInput, kAudioObjectPropertyElementMain };
        UInt32 streamSize = 0;
        if (AudioObjectGetPropertyDataSize(devices[i], &streams, 0, NULL, &streamSize) == noErr && streamSize > 0) [result addObject:@(devices[i])];
    }
    return result;
}
static AudioDeviceID DeviceWithUID(NSString *uid, bool output) {
    for (NSNumber *number in Devices(output)) {
        if ([DeviceString(number.unsignedIntValue, kAudioDevicePropertyDeviceUID) isEqualToString:uid]) return number.unsignedIntValue;
    }
    return kAudioObjectUnknown;
}
char *copilot_audio_devices(bool output) {
    @autoreleasepool {
        AudioDeviceID defaultDevice = DefaultDevice(output);
        NSMutableArray *rows = [NSMutableArray array];
        for (NSNumber *number in Devices(output)) {
            AudioDeviceID device = number.unsignedIntValue;
            NSString *uid = DeviceString(device, kAudioDevicePropertyDeviceUID);
            if (!uid) continue;
            NSString *name = DeviceString(device, kAudioObjectPropertyName) ?: uid;
            [rows addObject:@{ @"id": [@"coreaudio:" stringByAppendingString:uid], @"name": name, @"is_default": @(device == defaultDevice) }];
        }
        NSData *json = [NSJSONSerialization dataWithJSONObject:rows options:0 error:nil];
        return strdup(json ? [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding].UTF8String : "[]");
    }
}
void copilot_audio_free(char *value) { free(value); }

@interface CopilotAudio : NSObject <SCStreamOutput, SCStreamDelegate>
@property (atomic, assign) BOOL active;
@property (strong) dispatch_queue_t queue;
@property (strong) dispatch_source_t monitor;
@property (strong) SCStream *stream;
@property AudioQueueRef microphone;
@property void *context;
@property SamplesFn samples;
@property ErrorFn error;
@property (strong) NSMutableData *interleaved;
@end

@implementation CopilotAudio
- (void)fail:(NSString *)message {
    if (self.active) self.error(self.context, message.UTF8String);
}
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    // Serialize errors with data delivery. Late completions after stop see inactive.
    dispatch_async(self.queue, ^{ [self fail:[@"System audio capture stopped: " stringByAppendingString:error.localizedDescription]]; });
}
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(SCStreamOutputType)type {
    if (!self.active || type != SCStreamOutputTypeAudio || !CMSampleBufferIsValid(sample) || !CMSampleBufferDataIsReady(sample)) return;
    const AudioStreamBasicDescription *format = CMAudioFormatDescriptionGetStreamBasicDescription(CMSampleBufferGetFormatDescription(sample));
    CMItemCount frames = CMSampleBufferGetNumSamples(sample);
    if (!format || frames <= 0) return;
    if (format->mFormatID != kAudioFormatLinearPCM || !(format->mFormatFlags & kAudioFormatFlagIsFloat)
        || (format->mFormatFlags & kAudioFormatFlagIsBigEndian) || format->mBitsPerChannel != 32
        || format->mChannelsPerFrame < 1 || format->mChannelsPerFrame > 2 || !isfinite(format->mSampleRate)
        || format->mSampleRate < 8000 || format->mSampleRate > 192000 || frames > format->mSampleRate) {
        [self fail:@"ScreenCaptureKit supplied an unsupported audio format."]; return;
    }
    // The requested stereo Float32 format needs at most two planar buffers.
    struct { AudioBufferList list; AudioBuffer second; } storage = {0};
    CMBlockBufferRef block = NULL;
    OSStatus result = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample, NULL,
        &storage.list, sizeof(storage), kCFAllocatorDefault, kCFAllocatorDefault,
        kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment, &block);
    if (result != noErr) {
        if (block) CFRelease(block);
        [self fail:Status(@"Could not read system audio", result)];
        return;
    }
    AudioBufferList *buffers = &storage.list;
    UInt32 channels = format->mChannelsPerFrame;
    BOOL planar = (format->mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0;
    BOOL valid = buffers->mNumberBuffers == (planar ? channels : 1);
    for (UInt32 i = 0; valid && i < buffers->mNumberBuffers; i++) {
        AudioBuffer buffer = buffers->mBuffers[i];
        UInt32 expectedChannels = planar ? 1 : channels;
        valid = buffer.mData && buffer.mNumberChannels == expectedChannels
            && buffer.mDataByteSize >= (size_t)frames * expectedChannels * sizeof(float);
    }
    if (valid && self.active) {
        if (planar && channels > 1) {
            self.interleaved.length = (size_t)frames * channels * sizeof(float);
            float *dest = self.interleaved.mutableBytes;
            for (CMItemCount frame = 0; frame < frames; frame++) {
                for (UInt32 channel = 0; channel < channels; channel++) {
                    dest[frame * channels + channel] = ((const float *)buffers->mBuffers[channel].mData)[frame];
                }
            }
            self.samples(self.context, false, dest, (size_t)frames * channels, (uint32_t)format->mSampleRate, (uint16_t)channels);
        } else {
            self.samples(self.context, false, buffers->mBuffers[0].mData, (size_t)frames * channels, (uint32_t)format->mSampleRate, (uint16_t)channels);
        }
    } else if (!valid) { [self fail:@"ScreenCaptureKit supplied an incomplete audio buffer."]; }
    if (block) CFRelease(block);
}
@end

// Poll on the callback queue: a device can disappear without producing another buffer.
// The weak capture avoids a timer/owner cycle; Stop drains this queue before disposal.
static BOOL MonitorMicrophone(CopilotAudio *owner, AudioDeviceID device) {
    dispatch_source_t monitor = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, owner.queue);
    if (!monitor) return NO;
    owner.monitor = monitor;
    __weak CopilotAudio *weakOwner = owner;
    __block BOOL reported = NO;
    __block unsigned stoppedTicks = 0;
    dispatch_source_set_timer(monitor, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), NSEC_PER_SEC / 2, NSEC_PER_MSEC * 100);
    dispatch_source_set_event_handler(monitor, ^{
        CopilotAudio *capture = weakOwner;
        if (!capture.active || reported) return;
        NSString *problem = nil;
        AudioObjectPropertyAddress property = { kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
        UInt32 alive = 0, size = sizeof(alive);
        OSStatus status = AudioObjectGetPropertyData(device, &property, 0, NULL, &size, &alive);
        if (status != noErr || !alive) {
            problem = @"The selected microphone disconnected. Reconnect it or choose another microphone, then restart the call.";
        } else {
            OSStatus conversion = noErr;
            size = sizeof(conversion);
            if (AudioQueueGetProperty(capture.microphone, kAudioQueueProperty_ConverterError, &conversion, &size) == noErr && conversion != noErr) {
                problem = Status(@"Microphone conversion stopped", conversion);
            } else {
                UInt32 running = 0;
                size = sizeof(running);
                status = AudioQueueGetProperty(capture.microphone, kAudioQueueProperty_IsRunning, &running, &size);
                if (status != noErr) problem = Status(@"Could not check the microphone", status);
                else if (running) stoppedTicks = 0;
                // Allow delayed device startup, but never leave a stopped queue silent.
                else if (++stoppedTicks >= 6) problem = @"The microphone stopped capturing audio. Check the device and restart the call.";
            }
        }
        if (problem) { reported = YES; [capture fail:problem]; }
    });
    dispatch_resume(monitor);
    return YES;
}
// This always runs on the Rust capture worker, never the stream callback queue or main.
static void Stop(CopilotAudio *owner) {
    owner.active = NO;
    if (owner.monitor) {
        dispatch_source_cancel(owner.monitor);
        owner.monitor = nil;
    }
    // A health check may already be reading the queue/device. Finish it before freeing
    // the AudioQueue; inactive late timer/data callbacks cannot access the native handle.
    dispatch_sync(owner.queue, ^{});
    if (owner.microphone) {
        AudioQueueStop(owner.microphone, true);
        AudioQueueDispose(owner.microphone, true);
        owner.microphone = NULL;
    }
    SCStream *stream = owner.stream;
    if (stream) {
        dispatch_semaphore_t stopped = dispatch_semaphore_create(0);
        [stream stopCaptureWithCompletionHandler:^(NSError *error) { dispatch_semaphore_signal(stopped); }];
        dispatch_semaphore_wait(stopped, dispatch_time(DISPATCH_TIME_NOW, StopWait));
        [stream removeStreamOutput:owner type:SCStreamOutputTypeAudio error:nil];
        owner.stream = nil;
    }
    // Any late stream/delegate callbacks retain owner and check active before touching
    // Rust. Drain callbacks that passed the active check before freeing the Rust box.
    dispatch_sync(owner.queue, ^{});
}
static BOOL MicrophonePermission(char **failure) {
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
    if (status == AVAuthorizationStatusAuthorized) return YES;
    if (status == AVAuthorizationStatusNotDetermined) {
        dispatch_semaphore_t ready = dispatch_semaphore_create(0);
        __block BOOL allowed = NO;
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL granted) { allowed = granted; dispatch_semaphore_signal(ready); }];
        if (dispatch_semaphore_wait(ready, dispatch_time(DISPATCH_TIME_NOW, StartWait)) != 0) {
            Failure(failure, @"Microphone permission is still pending. Approve it, then start the call again."); return NO;
        }
        if (allowed) return YES;
    }
    Failure(failure, @"Allow Microphone access for Career Pilot in System Settings > Privacy & Security, then try again.");
    return NO;
}
static AudioStreamBasicDescription PCM(double rate) {
    AudioStreamBasicDescription format = {0};
    format.mSampleRate = rate;
    format.mFormatID = kAudioFormatLinearPCM;
    format.mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked;
    format.mBytesPerPacket = sizeof(float);
    format.mFramesPerPacket = 1;
    format.mBytesPerFrame = sizeof(float);
    format.mChannelsPerFrame = 1;
    format.mBitsPerChannel = 32;
    return format;
}

void *copilot_audio_start(const char *microphoneUID, void *context, SamplesFn samples, ErrorFn error, char **failure) {
    @autoreleasepool {
        if (!microphoneUID || !context || !samples || !error) { Failure(failure, @"Invalid audio capture request."); return NULL; }
        if (!MicrophonePermission(failure)) return NULL;
        NSString *uid = [NSString stringWithUTF8String:microphoneUID];
        AudioDeviceID device = DeviceWithUID(uid, false);
        if (device == kAudioObjectUnknown) { Failure(failure, @"The selected microphone is no longer connected. Choose an available microphone."); return NULL; }
        CopilotAudio *owner = [CopilotAudio new];
        owner.context = context; owner.samples = samples; owner.error = error;
        owner.queue = dispatch_queue_create("com.copilot.audio.capture", DISPATCH_QUEUE_SERIAL);
        owner.interleaved = [NSMutableData data];
        owner.active = YES;
        AudioObjectPropertyAddress rateProperty = { kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain };
        Float64 rate = 48000;
        UInt32 rateSize = sizeof(rate);
        AudioObjectGetPropertyData(device, &rateProperty, 0, NULL, &rateSize, &rate);
        if (!isfinite(rate) || rate < 8000 || rate > 192000) { Failure(failure, @"The microphone sample rate is not supported."); Stop(owner); return NULL; }
        AudioStreamBasicDescription format = PCM(rate);
        AudioQueueRef microphone = NULL;
        OSStatus status = AudioQueueNewInputWithDispatchQueue(&microphone, &format, 0, owner.queue,
            ^(AudioQueueRef queue, AudioQueueBufferRef buffer, const AudioTimeStamp *time, UInt32 packets, const AudioStreamPacketDescription *descriptions) {
                if (!owner.active) return;
                if (buffer->mAudioDataByteSize % sizeof(float) == 0) {
                    owner.samples(owner.context, true, buffer->mAudioData, buffer->mAudioDataByteSize / sizeof(float), (uint32_t)rate, 1);
                }
                if (owner.active) {
                    OSStatus enqueue = AudioQueueEnqueueBuffer(queue, buffer, 0, NULL);
                    if (enqueue != noErr) [owner fail:Status(@"Microphone capture stopped", enqueue)];
                }
            });
        owner.microphone = microphone;
        if (status == noErr) {
            CFStringRef nativeUID = (__bridge CFStringRef)uid;
            status = AudioQueueSetProperty(microphone, kAudioQueueProperty_CurrentDevice, &nativeUID, sizeof(nativeUID));
        }
        for (int i = 0; status == noErr && i < 3; i++) {
            AudioQueueBufferRef buffer = NULL;
            // Twenty milliseconds per callback. AudioQueue converts the device to mono.
            status = AudioQueueAllocateBuffer(microphone, (UInt32)(rate / 50) * sizeof(float), &buffer);
            if (status == noErr) status = AudioQueueEnqueueBuffer(microphone, buffer, 0, NULL);
        }
        if (status != noErr) { Failure(failure, Status(@"Could not open the selected microphone", status)); Stop(owner); return NULL; }

        dispatch_semaphore_t ready = dispatch_semaphore_create(0);
        __block SCShareableContent *content = nil;
        __block NSError *problem = nil;
        [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:NO completionHandler:^(SCShareableContent *value, NSError *err) {
            content = value; problem = err; dispatch_semaphore_signal(ready);
        }];
        if (dispatch_semaphore_wait(ready, dispatch_time(DISPATCH_TIME_NOW, StartWait)) != 0) {
            Failure(failure, @"System audio permission is still pending. Allow Screen Recording, then start the call again."); Stop(owner); return NULL;
        }
        SCDisplay *display = content.displays.firstObject;
        for (SCDisplay *candidate in content.displays) if (candidate.displayID == CGMainDisplayID()) { display = candidate; break; }
        if (!display) {
            Failure(failure, problem ? [@"System audio needs Screen Recording permission: " stringByAppendingString:problem.localizedDescription] : @"No display is available for system audio capture.");
            Stop(owner); return NULL;
        }
        SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:display excludingApplications:@[] exceptingWindows:@[]];
        SCStreamConfiguration *config = [SCStreamConfiguration new];
        config.capturesAudio = YES;
        config.excludesCurrentProcessAudio = NO; // Preserve guest voice and audio-check tone capture.
        config.sampleRate = 48000;
        config.channelCount = 2;
        // Only an audio output is registered; no screen frames leave this stream.
        config.width = 2; config.height = 2;
        config.minimumFrameInterval = CMTimeMake(1, 1);
        config.showsCursor = NO;
        owner.stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:owner];
        if (![owner.stream addStreamOutput:owner type:SCStreamOutputTypeAudio sampleHandlerQueue:owner.queue error:&problem]) {
            Failure(failure, problem.localizedDescription); Stop(owner); return NULL;
        }
        SCStream *stream = owner.stream;
        [stream startCaptureWithCompletionHandler:^(NSError *err) {
            problem = err;
            if (!owner.active && !err) [stream stopCaptureWithCompletionHandler:^(NSError *ignored) {}];
            dispatch_semaphore_signal(ready);
        }];
        long startResult = dispatch_semaphore_wait(ready, dispatch_time(DISPATCH_TIME_NOW, StartWait));
        if (startResult != 0) {
            // The completion may still write problem. Read it only after its signal.
            Failure(failure, @"System audio capture timed out. Check Screen Recording permission and try again.");
            Stop(owner); return NULL;
        }
        if (problem) {
            Failure(failure, [@"Could not capture system audio: " stringByAppendingString:problem.localizedDescription]);
            Stop(owner); return NULL;
        }
        status = AudioQueueStart(microphone, NULL);
        if (status != noErr) { Failure(failure, Status(@"Could not start the microphone", status)); Stop(owner); return NULL; }
        if (!MonitorMicrophone(owner, device)) {
            Failure(failure, @"Could not monitor the microphone. Start the call again."); Stop(owner); return NULL;
        }
        return (__bridge_retained void *)owner;
    }
}
void copilot_audio_stop(void *handle) {
    if (!handle) return;
    @autoreleasepool { CopilotAudio *owner = (__bridge_transfer CopilotAudio *)handle; Stop(owner); }
}

bool copilot_audio_tone(const char *outputUID, const float *samples, size_t count, uint32_t rate, char **failure) {
    @autoreleasepool {
        if (!outputUID || !samples || !count || count > 1920000 || rate < 8000 || rate > 192000) { Failure(failure, @"Invalid test tone."); return false; }
        NSString *uid = [NSString stringWithUTF8String:outputUID];
        if (DeviceWithUID(uid, true) == kAudioObjectUnknown) { Failure(failure, @"The selected playback output is no longer connected."); return false; }
        AudioStreamBasicDescription format = PCM(rate);
        dispatch_queue_t queue = dispatch_queue_create("com.copilot.audio.tone", DISPATCH_QUEUE_SERIAL);
        dispatch_semaphore_t played = dispatch_semaphore_create(0);
        AudioQueueRef output = NULL;
        OSStatus status = AudioQueueNewOutputWithDispatchQueue(&output, &format, 0, queue,
            ^(AudioQueueRef audio, AudioQueueBufferRef buffer) { dispatch_semaphore_signal(played); });
        if (status == noErr) {
            CFStringRef nativeUID = (__bridge CFStringRef)uid;
            status = AudioQueueSetProperty(output, kAudioQueueProperty_CurrentDevice, &nativeUID, sizeof(nativeUID));
        }
        AudioQueueBufferRef buffer = NULL;
        if (status == noErr) status = AudioQueueAllocateBuffer(output, (UInt32)(count * sizeof(float)), &buffer);
        if (status == noErr) {
            memcpy(buffer->mAudioData, samples, count * sizeof(float));
            buffer->mAudioDataByteSize = (UInt32)(count * sizeof(float));
            status = AudioQueueEnqueueBuffer(output, buffer, 0, NULL);
        }
        if (status == noErr) status = AudioQueueStart(output, NULL);
        BOOL complete = NO;
        if (status == noErr) {
            int64_t duration = (int64_t)count * NSEC_PER_SEC / rate;
            dispatch_time_t earliestStop = dispatch_time(DISPATCH_TIME_NOW, duration + NSEC_PER_SEC / 4);
            complete = dispatch_semaphore_wait(played, dispatch_time(DISPATCH_TIME_NOW, duration + StopWait)) == 0;
            if (complete) {
                // A reusable buffer does not mean its audio has reached the device.
                // Keep playback alive for the entire tone plus hardware padding from
                // AudioQueueStart, even when the buffer is returned immediately.
                dispatch_semaphore_wait(dispatch_semaphore_create(0), earliestStop);
            }
        }
        if (output) { AudioQueueStop(output, true); AudioQueueDispose(output, true); }
        dispatch_sync(queue, ^{});
        if (status != noErr) Failure(failure, Status(@"Could not play the tone on the selected output", status));
        else if (!complete) Failure(failure, @"The selected playback output did not finish the test tone.");
        return status == noErr && complete;
    }
}