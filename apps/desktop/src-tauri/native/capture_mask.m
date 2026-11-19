// Experimental capture exclusion for our own windows. No capture-app hooks or private API.
// AVFoundation documents preventsCapture for video, not masks over arbitrary WebKit UI.
// Actual receiver-side meeting tests are required before claiming compatibility.
#import <AppKit/AppKit.h>
#import <AVFoundation/AVFoundation.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#include <stdint.h>
#include <string.h>

static char CopilotCaptureMaskKey;

@interface CopilotCaptureMask : NSObject
@property (weak) NSWindow *window;
@property (weak) NSView *view;
@property (strong) CALayer *previousMask;
@property (strong) AVSampleBufferDisplayLayer *shield;
@property (strong) NSColor *background;
@property BOOL opaque;
@property BOOL shadow;
@property BOOL active;
@property (strong) NSTimer *timer;
@property (strong) id wakeObserver;
- (BOOL)install:(NSWindow *)window;
- (void)remove;
- (BOOL)prime;
- (void)refresh:(NSNotification *)notification;
@end

@implementation CopilotCaptureMask
- (BOOL)prime {
    // One opaque image supplies mask alpha. A layer background color alone is not video
    // content and does not exercise preventsCapture. All CF objects have bounded ownership.
    CVPixelBufferRef pixels = NULL;
    NSDictionary *attributes = @{(__bridge NSString *)kCVPixelBufferIOSurfacePropertiesKey: @{}};
    if (CVPixelBufferCreate(kCFAllocatorDefault, 2, 2, kCVPixelFormatType_32BGRA,
                           (__bridge CFDictionaryRef)attributes, &pixels) != kCVReturnSuccess)
        return NO;
    CVReturn lock = CVPixelBufferLockBaseAddress(pixels, 0);
    if (lock != kCVReturnSuccess) { CFRelease(pixels); return NO; }
    void *base = CVPixelBufferGetBaseAddress(pixels);
    if (base) memset(base, 255, CVPixelBufferGetDataSize(pixels));
    CVPixelBufferUnlockBaseAddress(pixels, 0);
    if (!base) { CFRelease(pixels); return NO; }

    CMVideoFormatDescriptionRef format = NULL;
    CMSampleBufferRef sample = NULL;
    OSStatus result = CMVideoFormatDescriptionCreateForImageBuffer(kCFAllocatorDefault, pixels, &format);
    if (result == noErr) {
        CMSampleTimingInfo timing = {kCMTimeInvalid, kCMTimeZero, kCMTimeInvalid};
        result = CMSampleBufferCreateReadyWithImageBuffer(kCFAllocatorDefault, pixels,
                                                         format, &timing, &sample);
    }
    if (result == noErr && sample) {
        CFArrayRef attachments = CMSampleBufferGetSampleAttachmentsArray(sample, true);
        if (attachments && CFArrayGetCount(attachments)) {
            CFMutableDictionaryRef entry = (CFMutableDictionaryRef)CFArrayGetValueAtIndex(attachments, 0);
            CFDictionarySetValue(entry, kCMSampleAttachmentKey_DisplayImmediately, kCFBooleanTrue);
        }
        [self.shield.sampleBufferRenderer enqueueSampleBuffer:sample];
    }
    if (sample) CFRelease(sample);
    if (format) CFRelease(format);
    CFRelease(pixels);
    return result == noErr && self.shield.sampleBufferRenderer.status != AVQueuedSampleBufferRenderingStatusFailed;
}

- (BOOL)install:(NSWindow *)window {
    // Include the native title bar in the mask, not just the WKWebView. Otherwise an
    // apparently empty window frame could still be visible in the meeting stream.
    NSView *view = window.contentView.superview ?: window.contentView;
    if (!view) return NO;
    self.window = window;
    self.view = view;
    view.wantsLayer = YES;
    if (!view.layer) return NO;
    self.previousMask = view.layer.mask;
    self.background = window.backgroundColor;
    self.opaque = window.opaque;
    self.shadow = window.hasShadow;
    self.shield = [AVSampleBufferDisplayLayer layer];
    self.shield.videoGravity = AVLayerVideoGravityResize;
    self.shield.preventsCapture = YES;
    if (![self prime]) return NO;

    self.active = YES;
    window.opaque = NO;
    window.backgroundColor = NSColor.clearColor;
    window.hasShadow = NO;
    [self refresh:nil];
    NSNotificationCenter *center = NSNotificationCenter.defaultCenter;
    for (NSNotificationName name in @[NSWindowDidResizeNotification,
                                      NSWindowDidChangeBackingPropertiesNotification,
                                      NSWindowDidDeminiaturizeNotification,
                                      NSWindowDidBecomeKeyNotification]) {
        [center addObserver:self selector:@selector(refresh:) name:name object:window];
    }
    [center addObserver:self selector:@selector(closing:) name:NSWindowWillCloseNotification object:window];
    __weak CopilotCaptureMask *weakSelf = self;
    self.wakeObserver = [NSWorkspace.sharedWorkspace.notificationCenter
        addObserverForName:NSWorkspaceDidWakeNotification object:nil queue:NSOperationQueue.mainQueue
        usingBlock:^(NSNotification *note) { [weakSelf refresh:note]; }];
    // Keep the single video frame alive/recoverable across renderer resets. A weak timer
    // callback avoids retaining a closed window's controller indefinitely.
    self.timer = [NSTimer timerWithTimeInterval:1 repeats:YES block:^(NSTimer *timer) {
        [weakSelf refresh:nil];
    }];
    [NSRunLoop.mainRunLoop addTimer:self.timer forMode:NSRunLoopCommonModes];
    return YES;
}

- (void)refresh:(NSNotification *)notification {
    if (!self.active) return;
    NSWindow *window = self.window;
    NSView *view = self.view;
    if (!window || !view) { [self remove]; return; }
    AVSampleBufferVideoRenderer *renderer = self.shield.sampleBufferRenderer;
    BOOL wake = [notification.name isEqualToString:NSWorkspaceDidWakeNotification];
    if (wake || renderer.requiresFlushToResumeDecoding || renderer.status == AVQueuedSampleBufferRenderingStatusFailed) {
        [renderer flush];
        if (![self prime]) {
            // Recover usability, not a false guarantee of capture protection.
            NSLog(@"Copilot: experimental capture mask failed (%@); restoring visible content. Stop screen sharing and retry Stealth mode.", renderer.error);
            [self remove];
            return;
        }
    }
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    self.shield.frame = view.layer.bounds;
    self.shield.contentsScale = window.backingScaleFactor;
    view.layer.mask = self.shield;
    [CATransaction commit];
}

- (void)closing:(NSNotification *)notification { [self remove]; }

- (void)remove {
    [self.timer invalidate];
    self.timer = nil;
    [NSNotificationCenter.defaultCenter removeObserver:self];
    if (self.wakeObserver) {
        [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self.wakeObserver];
        self.wakeObserver = nil;
    }
    if (!self.active) return;
    self.active = NO;
    if (self.view.layer.mask == self.shield) self.view.layer.mask = self.previousMask;
    self.window.backgroundColor = self.background;
    self.window.opaque = self.opaque;
    self.window.hasShadow = self.shadow;
    [self.shield.sampleBufferRenderer flush];
}

- (void)dealloc {
    [self.timer invalidate];
    [NSNotificationCenter.defaultCenter removeObserver:self];
    if (self.wakeObserver)
        [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self.wakeObserver];
}
@end

// Called only inside Tauri's with_webview callback, on AppKit's main thread.
int32_t copilot_capture_mask_set(void *handle, uint8_t enabled) {
    if (![NSThread isMainThread] || !handle) return -1;
    NSWindow *window = (__bridge NSWindow *)handle;
    CopilotCaptureMask *mask = objc_getAssociatedObject(window, &CopilotCaptureMaskKey);
    if (!enabled) {
        [mask remove];
        objc_setAssociatedObject(window, &CopilotCaptureMaskKey, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        return 0;
    }
    if (mask.active) { [mask refresh:nil]; return mask.active ? 0 : -2; }
    mask = [CopilotCaptureMask new];
    if (![mask install:window]) return -2;
    objc_setAssociatedObject(window, &CopilotCaptureMaskKey, mask, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    return 0;
}
