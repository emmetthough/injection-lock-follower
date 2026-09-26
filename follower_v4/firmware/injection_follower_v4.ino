/*
 Repeated Peak-Finding Fabry-Perot Scan
 Arduino Due Version
*/
#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <Arduino.h>

#define N 500              // Samples per scan

#define ANALOG_PIN A0
#define TRIGGER_PIN 53
#define SLOWER_DAC DAC0
#define XBEAM_DAC DAC1

#define DIGILOCK_STATUS 23
#define SLOWER_ENABLE 24
#define XBEAM_ENABLE 25
#define SLOWER_STATUS 26
#define XBEAM_STATUS 27
#define SLOWER_FAILURE 28
#define XBEAM_FAILURE 29

#define HIGH_THRESH 800
#define LOW_THRESH 400

#define NUM_PEAKS 20
#define MAX_SCANS 100
#define NOISE_THRESH 100

#define CLUSTER_GAP 75
#define MAX_CLUSTERS 3

//#define XBEAM_MAX 2948
//#define XBEAM_MIN 1148

volatile bool debug = false;

// ---------- Holdoff drift servo ----------
// Peaks slowly walk through the acquisition window over hours. Once a peak approaches the
// edge of its search region the tracked maximum is a point on its flank rather than its
// top, so the measured height falls even though the lock is fine -- position drift
// masquerading as height loss. This servo corrects the trigger holdoff to hold the peaks
// where init found them, without touching refHeight.
// ---------- Safe-side bias and instability back-off ----------
// The maximum of the SPC is not a safe operating point for a channel whose sharp edge sits
// next to it: the servo walks back to the peak after every excursion and parks beside the
// cliff. These bias the operating point onto the gentle flank instead.
float bias_frac        = 0.25;   // fixed offset, as a fraction of fwhm
int   instab_window_ms = 60000;  // rolling window for counting instability events
int   instab_events    = 3;      // events in window that trigger a back-off
float backoff_step     = 0.02;   // drop in losingThresh per trigger
float backoff_floor    = 0.70;   // hard minimum; latches a fault here
unsigned long backoff_quiet_ms = 600000; // quiet time before creeping back
float backoff_creep    = 0.005;  // increase per quiet interval

bool  holdoff_fb_enabled   = false;
float holdoff_slope        = 0.0f;   // samples per us, negative. 0 means uncalibrated.
int   holdoff_cal_step     = 10;     // us per calibration step
float holdoff_cal_frac     = 0.5;    // calibrate until displacement reaches frac*SEARCH_WINDOW
float holdoff_actuate_frac = 0.5;    // actuate when |error| exceeds frac*SEARCH_WINDOW
int   holdoff_max_step     = 20;     // us, clamp on a single correction
int   holdoff_min          = 0;
int   holdoff_max          = 5000;
unsigned long holdoff_interval_ms = 60000;
#define HOLDOFF_CAL_MAX_STEPS 16
#define HOLDOFF_CAL_SCANS     5

// Position error accumulator, filled by trackPeaksInRegions at no extra cost.
double holdoffErrSum = 0.0;
long   holdoffErrN   = 0;
float  lastHoldoffErr = 0.0f;   // most recent drained average, for reporting
bool   lastHoldoffErrValid = false;

// Scan acquisition watchdog. Must comfortably exceed one ramp period; 500 ms covers scan
// rates down to ~2 Hz. Adjustable at runtime with "Cscan_timeout_ms,<value>".
unsigned long scan_timeout_us = 500000UL;
bool trigger_fail = false;

// Bounded spin for the ADC end-of-conversion flag. At PRESCAL(14) a conversion takes
// roughly 7 us, so this is ~2 orders of magnitude of headroom before it trips.
#define ADC_EOC_MAX_SPINS 100000UL

volatile uint16_t buffer[N];
volatile int adcIndex = 0;
volatile int delayus = 200;

// Per-channel state now lives in the Channel struct below. Legacy names are kept as
// reference aliases so the reporting/command code did not have to be rewritten too.

bool SPC_init = false;

float losing_meanThresh = 0.95;
float lost_meanThresh = 0.25;
float losing_stdThresh = 2.0;
float relock_thresh = 0.95;
float bump_thresh = 0.05;

int NRECOVERY = 5;

bool feedbackActive = false;

// Storage for all found peaks
uint16_t highPeaks[NUM_PEAKS];
uint16_t lowPeaks[NUM_PEAKS];
uint16_t initialPeaks[NUM_PEAKS];

uint16_t highPeakPos[NUM_PEAKS];
uint16_t lowPeakPos[NUM_PEAKS];
uint16_t initialPeakPos[NUM_PEAKS];

int foundHigh = 0;
int foundLow = 0;
int foundInitial = 0;
int SEARCH_WINDOW = 50;

uint16_t currentHighVals[MAX_CLUSTERS];
uint16_t currentLowVals[MAX_CLUSTERS];

// Number of scans used to characterise the reference height/spread at init.
#define STATS_SCANS 30

int currentHighPos[MAX_CLUSTERS];
int currentLowPos[MAX_CLUSTERS];

bool highPeakLost[MAX_CLUSTERS];
bool lowPeakLost[MAX_CLUSTERS];

float highClusterMeans[MAX_CLUSTERS];
float lowClusterMeans[MAX_CLUSTERS];

bool initialized = false;

// ---------- Peak statistics structure ----------
struct PeakStats {
 float meanHeight;
 float stdHeight;
};

struct Slope {
  float m;
  int x0;
  uint16_t y0;
};

// ---------- Global stats ----------
PeakStats highStats;
PeakStats lowStats;


const int BUFFER_SIZE = 5;
const int AVG_BUFFER_SIZE = 3;

struct RunningBuffer {
  uint16_t* data;    // Pointer to dynamically allocated buffer
  int size;          // Max number of samples
  int head;          // Next index to write
  int count;         // Current number of elements

  float mean;        // Running mean
  float M2;          // Sum of squared differences

  RunningBuffer(int n) : size(n), head(0), count(0), mean(0.0f), M2(0.0f) {
    data = new uint16_t[n];
  }

  ~RunningBuffer() {
    delete[] data;
  }

  void push(uint16_t value) {
    data[head] = value;
    head = (head + 1) % size;
    if (count < size) count++;

    // Recompute mean and M2
    mean = 0.0f;
    for (int i = 0; i < count; i++) mean += get(i);
    mean /= count;

    M2 = 0.0f;
    for (int i = 0; i < count; i++) {
      float delta = (float)get(i) - mean;
      M2 += delta * delta;
    }
  }
  uint16_t get(int i) const {
    if (i >= count) return 0;
    int index = (head - count + i + size) % size;
    return data[index];
  }
  uint16_t latest() const {
    if (count == 0) return 0;
    int idx = (head - 1 + size) % size;
    return data[idx];
  }
  float getMean() const {
    return mean;
  }
  float getStd() const {
    if (count < 2) return 0.0f;
    return sqrt(M2 / (count - 1));
  }
};


RunningBuffer slowerRunningBuffer(BUFFER_SIZE);
RunningBuffer xbeamRunningBuffer(BUFFER_SIZE);

// ---------- Per-channel state ----------
// Everything that used to exist twice as slowerX / xbeamX. The control functions take a
// Channel& so each algorithm exists once and the two channels cannot drift apart again.
//
// fbSign is the key parameter: it encodes which way "toward the peak" is for this laser.
// The direction asymmetries that used to be hardcoded separately per channel all reduce
// to it -- relock accepts a correction when dx*fbSign < 0, and recovery sweeps in the
// -fbSign direction from a start offset of +fbSign*(attempts+1)*fwhm.
struct Channel {
  const char* name;
  int dacPin, statusPin, failurePin, enablePin;
  int fbSign;

  int  dacCode;            // absolute DAC code currently driven
  int  goodCode;           // last known-good absolute DAC code
  bool fail;
  bool atLimit;
  int  nRecoveryAttempts;

  float refHeight;         // init-time setpoint, reset by the "update peaks" button
  float refStd;
  int   fwhm;              // half-width in DAC LSBs, from the SPC fit
  Slope fb;                // SPC fit; only .m feeds the control law
  int   lastMean;          // cluster-averaged height from the last scan, -1 if none

  // Safe-side bias. fbSign points at the gentle flank, so +fbSign is always the safe
  // direction: high current for the slower, low current for the xbeam.
  bool  biasEnabled;       // fixed offset applied after each successful acquisition
  bool  contBumpEnabled;   // try a safe-side step on every locked iteration
  int   contBumpSkip;      // iterations to wait after a rejected continuous bump

  // Adaptive acting threshold. Backs off on detected instability, floors, creeps back.
  // Deliberately separate from refHeight, which also drives lost detection, relock's aim
  // point and recovery's success test.
  float losingThresh;
  bool  floorLatched;
  int   eventCount;
  unsigned long eventWindowStart;
  unsigned long lastEventMs;
  unsigned long lastCreepMs;

  RunningBuffer* buf;      // history of cluster-averaged heights
  float*     clusterMeans; // cluster positions within the scan
  uint16_t*  currentVals;
  int*       currentPos;
  bool*      peakLost;
  PeakStats* stats;
};

Channel slower = {
  "slower", SLOWER_DAC, SLOWER_STATUS, SLOWER_FAILURE, SLOWER_ENABLE, 1,
  2048, 2048, false, false, 0,
  0.0f, 0.0f, 0, {0.0f, 0, 0}, -1,
  true, false, 0,                         // bias on, continuous bump off
  0.95f, false, 0, 0, 0, 0,
  &slowerRunningBuffer, highClusterMeans, currentHighVals, currentHighPos,
  highPeakLost, &highStats
};

Channel xbeam = {
  "xbeam", XBEAM_DAC, XBEAM_STATUS, XBEAM_FAILURE, XBEAM_ENABLE, -1,
  2048, 2048, false, false, 0,
  0.0f, 0.0f, 0, {0.0f, 0, 0}, -1,
  false, false, 0,                        // bias off by default on the xbeam
  0.95f, false, 0, 0, 0, 0,
  &xbeamRunningBuffer, lowClusterMeans, currentLowVals, currentLowPos,
  lowPeakLost, &lowStats
};

// Legacy names, bound to the struct members. Lets the reporting, SPC and command-parsing
// code keep working unchanged while the control path uses Channel&. Note slowerADCout and
// slower_at_limit are no longer volatile: nothing writes them from an interrupt.
int   &slowerADCout             = slower.dacCode;
int   &xbeamADCout              = xbeam.dacCode;
bool  &slower_at_limit          = slower.atLimit;
bool  &xbeam_at_limit           = xbeam.atLimit;
int   &slower_fb_sign           = slower.fbSign;
int   &xbeam_fb_sign            = xbeam.fbSign;
int   &slowerFWHM               = slower.fwhm;
int   &xbeamFWHM                = xbeam.fwhm;
int   &slowerGoodADC            = slower.goodCode;
int   &xbeamGoodADC             = xbeam.goodCode;
bool  &slower_fail              = slower.fail;
bool  &xbeam_fail               = xbeam.fail;
int   &slower_nrecoveryAttempts = slower.nRecoveryAttempts;
int   &xbeam_nrecoveryAttempts  = xbeam.nRecoveryAttempts;
float &slowerHeight             = slower.refHeight;
float &slowerStd                = slower.refStd;
float &xbeamHeight              = xbeam.refHeight;
float &xbeamStd                 = xbeam.refStd;
Slope &slowerFbParams           = slower.fb;
Slope &xbeamFbParams            = xbeam.fb;
int   &lastSlowerMean           = slower.lastMean;
int   &lastXbeamMean            = xbeam.lastMean;

// ---------- Forward declarations ----------
// The Arduino IDE auto-generates these; declaring them explicitly lets the file be
// compiled by a plain toolchain for host-side testing.
void  sendPeaks();
void  sendTrace();
void  sendStats(PeakStats highStats, PeakStats lowStats);
void  sendClusters(int nHighClusters, int nLowClusters);
bool  acquireScan();
bool  trackPeaksInRegions();
void  initialize_peak_vals_locations();
Slope getSlope(int laser, float thresh, int nsteps, int baseCode,
               const int* steps, const uint16_t* peaks);
bool  check_Digilock();
bool  setDac(Channel &ch, int code);
float getIterationMean(Channel &ch);
void  bumpUp(Channel &ch);
void  relock(Channel &ch, int nattempts);
bool  recover(Channel &ch);
void  serviceChannel(Channel &ch);
void  feedbackWrapper();
void  applySafeBias(Channel &ch);
void  continuousBump(Channel &ch);
void  recordInstabilityEvent(Channel &ch);
void  backoffTick(Channel &ch);
void  resetBackoff(Channel &ch);
void  reportBackoff();
bool  measureHoldoffError(int nscans, float &out);
bool  calibrateHoldoff();
void  holdoffServo();
void  reportHoldoff();
void  spectralPurityCurve(float startmA, float stopmA, float stepuA);
void  parse_change_command(String args);
void  pinModeSetup();


// ---------- Helper: check if peak lost ----------
bool isPeakLost(float maxVal, float meanHeight, float stdHeight) {
 if (maxVal < NOISE_THRESH) return true;
 if (maxVal < (meanHeight - 2.0 * stdHeight)) return true;
 return false;
}

// ---------- ADC setup ----------
void setupADC() {
 ADC->ADC_CR = ADC_CR_SWRST;
 ADC->ADC_MR |= ADC_MR_PRESCAL(14);
 ADC->ADC_CHER = ADC_CHER_CH7;
}

// ---------- Scan acquisition ----------
// Returns false if the scan could not be acquired. Previously all three waits below were
// unbounded, so a stopped ramp generator, a powered-down scan driver or an unplugged
// trigger cable hung the board forever with the DAC frozen and the command path dead.
bool acquireScan() {
  const unsigned long t0 = micros();
  adcIndex = 0;

  // Wait for the trigger to fall...
  while (digitalRead(TRIGGER_PIN)) {
    if ((micros() - t0) > scan_timeout_us) { trigger_fail = true; return false; }
  }
  // ...then for the rising edge.
  while (!digitalRead(TRIGGER_PIN)) {
    if ((micros() - t0) > scan_timeout_us) { trigger_fail = true; return false; }
  }

  noInterrupts();
  delayMicroseconds(delayus); // holdoff
  interrupts();

  while (adcIndex < N) { // fill buffer
    ADC->ADC_CR = ADC_CR_START;
    unsigned long spins = 0;
    while (!(ADC->ADC_ISR & ADC_ISR_EOC7)) {
      if (++spins > ADC_EOC_MAX_SPINS) { trigger_fail = true; return false; }
    }
    buffer[adcIndex++] = ADC->ADC_CDR[7];
  }

  trigger_fail = false;
  return true;
}

// ---------- Local max detection ----------
bool isLocalMax(int i, int window) {
 uint16_t val = buffer[i];
 for (int j = -window; j <= window; j++) {
   if (j == 0) continue;
   if (buffer[i + j] > val) return false;
 }
 return true;
}

// ---------- Peak clustering / grouping ----------
// Takes positions by const pointer and sorts a LOCAL copy. Sorting the caller's array
// in place (as this used to) left highPeakPos[] reordered while highPeaks[] was not, so
// the two stopped describing the same peak at a given index and sendPeaks() reported
// mismatched val=/pos= pairs.
int clusterPeaks(const uint16_t *positions_in, int count, float *clusterMeans) {
 if (count <= 0) return 0;
 if (count > NUM_PEAKS) count = NUM_PEAKS;

 uint16_t positions[NUM_PEAKS];
 memcpy(positions, positions_in, count * sizeof(uint16_t));

 for (int i = 1; i < count; i++) {
   uint16_t key = positions[i];
   int j = i - 1;
   while (j >= 0 && positions[j] > key) {
     positions[j + 1] = positions[j];
     j--;
   }
   positions[j + 1] = key;
 }
 int clusterCount = 0;
 uint32_t sum = positions[0];
 int n = 1;
 for (int i = 1; i < count; i++) {
   if ((positions[i] - positions[i - 1]) > CLUSTER_GAP) {
     clusterMeans[clusterCount++] = (float)sum / n;
     sum = positions[i];
     n = 1;
     if (clusterCount >= MAX_CLUSTERS) break;
   } else {
     sum += positions[i];
     n++;
   }
 }
 if (clusterCount < MAX_CLUSTERS) {
   clusterMeans[clusterCount++] = (float)sum / n;
 }
 return clusterCount;
}

// ---------- Peak stats ----------
PeakStats computePeakStats(uint16_t *heights, uint16_t *positions, int count) {
 PeakStats s = {0, 0};
 if (count <= 0) return s;
 float sumH = 0;
 for (int i = 0; i < count; i++) sumH += heights[i];
 s.meanHeight = sumH / count;
 float varH = 0;
 for (int i = 0; i < count; i++) varH += pow(heights[i] - s.meanHeight, 2);
 if (count > 1) s.stdHeight = sqrt(varH / (count - 1));
 return s;
}

// ---------- Add new peaks ----------
void addNewPeaks() {
 int window = 5;
 for (int i = window; i < N - window; i++) {
   uint16_t val = buffer[i];
   if (val > HIGH_THRESH && isLocalMax(i, window)) {
     if (foundHigh < NUM_PEAKS) {
       highPeaks[foundHigh] = val;
       highPeakPos[foundHigh++] = i;
     }
   } else if (val > LOW_THRESH && val < HIGH_THRESH && isLocalMax(i, window)) {
     if (foundLow < NUM_PEAKS) {
       lowPeaks[foundLow] = val;
       lowPeakPos[foundLow++] = i;
     }
   }
 }
}

// ---------- Initialization ----------
void initialize_peak_vals_locations() {
 foundHigh = 0;
 foundLow = 0;
 int scanCount = 0;

 // --- Clear previous peak data ---
 if (initialized) {
  memset(highPeaks,     0, sizeof(highPeaks));
  memset(lowPeaks,      0, sizeof(lowPeaks));
  memset(initialPeaks,  0, sizeof(initialPeaks));

  memset(highPeakPos,     0, sizeof(highPeakPos));
  memset(lowPeakPos,      0, sizeof(lowPeakPos));
  memset(initialPeakPos,  0, sizeof(initialPeakPos));

  memset(highClusterMeans, 0, sizeof(highClusterMeans));
  memset(lowClusterMeans,  0, sizeof(lowClusterMeans));

  memset(highPeakLost, 0, sizeof(highPeakLost));
  memset(lowPeakLost,  0, sizeof(lowPeakLost));

  // Reset computed stats
  highStats = {0, 0};
  lowStats  = {0, 0};
  slowerHeight = slowerStd = 0.0f;
  xbeamHeight  = xbeamStd  = 0.0f;
  SEARCH_WINDOW = 50;
 }

 while (scanCount < MAX_SCANS) {
   acquireScan();
   addNewPeaks();
   scanCount++;
 }

 highStats = computePeakStats(highPeaks, highPeakPos, foundHigh);
 lowStats  = computePeakStats(lowPeaks, lowPeakPos, foundLow);

 int nHighClusters = clusterPeaks(highPeakPos, foundHigh, highClusterMeans);
 int nLowClusters  = clusterPeaks(lowPeakPos,  foundLow,  lowClusterMeans);

 if (nHighClusters > 0 && nLowClusters > 0) {
   int diff = abs((int)highClusterMeans[0] - (int)lowClusterMeans[0]);
   SEARCH_WINDOW = max(5, diff / 2.5);
 }

 // --- Reference height / spread, measured the way the servo measures ---
 // The servo compares slowerRunningBuffer.getMean()/.getStd() against slowerHeight and
 // slowerStd. Those buffers hold ONE cluster-averaged height per scan, so the reference
 // has to be built from the same quantity. Deriving it from computePeakStats() instead
 // (statistics over individual peaks) makes the std systematically larger and leaves the
 // losing_stdThresh test comparing incommensurable quantities.
 //
 // This is the setpoint the "update peaks" button resets, so it is deliberately measured
 // fresh here and never taken from the SPC.
 {
   double sSum = 0, sSq = 0, xSum = 0, xSq = 0;
   int sN = 0, xN = 0;
   for (int k = 0; k < STATS_SCANS; k++) {
     trackPeaksInRegions();
     if (lastSlowerMean >= 0) { double v = lastSlowerMean; sSum += v; sSq += v*v; sN++; }
     if (lastXbeamMean  >= 0) { double v = lastXbeamMean;  xSum += v; xSq += v*v; xN++; }
   }
   if (sN > 0) {
     slowerHeight = (float)(sSum / sN);
     slowerStd = (sN > 1) ? (float)sqrt((sSq - sSum*sSum/sN) / (sN - 1)) : 0.0f;
   }
   if (xN > 0) {
     xbeamHeight = (float)(xSum / xN);
     xbeamStd = (xN > 1) ? (float)sqrt((xSq - xSum*xSum/xN) / (xN - 1)) : 0.0f;
   }
 }

 // The acting threshold is per channel and adaptive; re-seed it from the nominal value
 // whenever the setpoint is re-measured.
 slower.losingThresh = losing_meanThresh;
 xbeam.losingThresh  = losing_meanThresh;

 initialized = true;
 
 SerialUSB.println("[START] Initalization");
 sendPeaks();
 SerialUSB.println("[END] Peaks");
 SerialUSB.println("[START] Stats");
 // Report the servo reference, not the per-peak statistics, so the GUI shows the
 // setpoint actually in use. Wire format is unchanged.
 {
   PeakStats slowerRef = {slowerHeight, slowerStd};
   PeakStats xbeamRef  = {xbeamHeight, xbeamStd};
   sendStats(slowerRef, xbeamRef);
 }
 SerialUSB.println("[END] Stats");
 SerialUSB.println("[START] Clusters");
 sendClusters(nHighClusters, nLowClusters);
 SerialUSB.println("[END] Clusters");
 
}

void sendPeaks() {
  SerialUSB.println("BEGIN_peaks");

  // Send peak info
  for (int i = 0; i < NUM_PEAKS; i++) {
    if (highPeaks[i]>0){
      SerialUSB.print("High "); SerialUSB.print(i);
      SerialUSB.print(": val="); SerialUSB.print(highPeaks[i]);
      SerialUSB.print(" pos="); SerialUSB.println(highPeakPos[i]);
    }
  }
  for (int i = 0; i < NUM_PEAKS; i++) {
    if (lowPeaks[i] > 0) {
      SerialUSB.print("Low "); SerialUSB.print(i);
      SerialUSB.print(": val="); SerialUSB.print(lowPeaks[i]);
      SerialUSB.print(" pos="); SerialUSB.println(lowPeakPos[i]);
    }
  }

  SerialUSB.println("END_peaks");
}

void sendTrace() {
  SerialUSB.println("[START] Trace");
  SerialUSB.write((uint8_t*)buffer, N * sizeof(uint16_t));
  SerialUSB.flush();
}

void sendStats(PeakStats highStats, PeakStats lowStats){
  
  SerialUSB.println("BEGIN_stats");
  SerialUSB.print("meanHeight=");
  SerialUSB.print(highStats.meanHeight, 2);
  SerialUSB.print(" stdHeight=");
  SerialUSB.println(highStats.stdHeight, 2);
  
  SerialUSB.println("BEGIN_lowStats");
  SerialUSB.print("meanHeight=");
  SerialUSB.print(lowStats.meanHeight, 2);
  SerialUSB.print(" stdHeight=");
  SerialUSB.println(lowStats.stdHeight, 2);

  SerialUSB.println("END_stats");
}

void sendClusters(int nHighClusters, int nLowClusters) {
  SerialUSB.println("BEGIN_clusters");
//  SerialUSB.print("High clusters found: ");
//  SerialUSB.println(nHighClusters);
  for (int i = 0; i < nHighClusters; i++) {
    SerialUSB.print("High cluster "); SerialUSB.print(i);
    SerialUSB.print(" meanPos = ");
    SerialUSB.println(highClusterMeans[i], 2);
  }

//  SerialUSB.println("BEGIN_lowClusters");
//  SerialUSB.print("Low clusters found: ");
//  SerialUSB.println(nLowClusters);

  for (int i = 0; i < nLowClusters; i++) {
    SerialUSB.print("Low cluster "); SerialUSB.print(i);
    SerialUSB.print(" meanPos = ");
    SerialUSB.println(lowClusterMeans[i], 2);
  }
  SerialUSB.println("END_clusters");
}

// ---------- Active monitoring ----------
bool trackPeaksInRegions() {
 if (!acquireScan()) {
   // No fresh data. Leave the running buffers untouched: pushing the stale buffer's
   // contents (or a zero) would look exactly like a lost lock and drive feedback on
   // information that is no longer real.
   lastSlowerMean = -1;
   lastXbeamMean  = -1;
   return false;
 }

 // --- Track high peaks ---
 uint32_t slower_sum = 0;   // uint32: 3 clusters x 4095 fits in uint16, but this is
 int slower_mean_N = 0;     // one MAX_CLUSTERS bump away from overflowing.
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (highClusterMeans[i] <= 0) continue;
   int center = (int)highClusterMeans[i];
   int start = max(0, center - SEARCH_WINDOW);
   int end   = min(N - 1, center + SEARCH_WINDOW);

   uint16_t maxVal = 0;
   int maxPos = center;
   for (int j = start; j <= end; j++) {
     if (buffer[j] > maxVal) {
       maxVal = buffer[j];
       maxPos = j;
     }
   }
   currentHighVals[i] = maxVal;
   currentHighPos[i]  = maxPos;
   highPeakLost[i]    = isPeakLost(maxVal, highStats.meanHeight, highStats.stdHeight);
   slower_sum += maxVal;
   slower_mean_N ++;
   if (!highPeakLost[i]) { holdoffErrSum += (maxPos - highClusterMeans[i]); holdoffErrN++; }
 }

 // Guard the divide: slower_mean_N is 0 whenever no high cluster is populated (before
 // init, or after an init that found nothing). Pushing 0 into the buffer would look
 // exactly like a lost lock and immediately trigger feedback, so push nothing instead.
 if (slower_mean_N > 0) {
   lastSlowerMean = (int)(slower_sum / slower_mean_N);
   slowerRunningBuffer.push((uint16_t)lastSlowerMean);
 } else {
   lastSlowerMean = -1;
 }

 uint32_t xbeam_sum = 0;
 int xbeam_mean_N = 0;
 // --- Track low peaks ---
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (lowClusterMeans[i] <= 0) continue;
   int center = (int)lowClusterMeans[i];
   int start = max(0, center - SEARCH_WINDOW);
   int end   = min(N - 1, center + SEARCH_WINDOW);

   uint16_t maxVal = 0;
   int maxPos = center;
   for (int j = start; j <= end; j++) {
     if (buffer[j] > maxVal) {
       maxVal = buffer[j];
       maxPos = j;
     }
   }
   currentLowVals[i] = maxVal;
   currentLowPos[i]  = maxPos;
   lowPeakLost[i]    = isPeakLost(maxVal, lowStats.meanHeight, lowStats.stdHeight);
   xbeam_sum += maxVal;
   xbeam_mean_N ++;
   if (!lowPeakLost[i]) { holdoffErrSum += (maxPos - lowClusterMeans[i]); holdoffErrN++; }
 }

 if (xbeam_mean_N > 0) {
   lastXbeamMean = (int)(xbeam_sum / xbeam_mean_N);
   xbeamRunningBuffer.push((uint16_t)lastXbeamMean);
 } else {
   lastXbeamMean = -1;
 }

 return true;
}

// ---------- Status printing ----------
void printPeakStatus() {
 SerialUSB.println("[START] peak_tracking");
 SerialUSB.println("BEGIN_tracking");
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (highClusterMeans[i] > 0) {
     SerialUSB.print("High "); SerialUSB.print(i);
     SerialUSB.print(" pos="); SerialUSB.print(currentHighPos[i]);
     SerialUSB.print(" val="); SerialUSB.print(currentHighVals[i]);
     SerialUSB.print(" status=");
     SerialUSB.println(highPeakLost[i] ? "LOST" : "OK");
   }
 }
 for (int i = 0; i < MAX_CLUSTERS; i++) {
   if (lowClusterMeans[i] > 0) {
     SerialUSB.print("Low "); SerialUSB.print(i);
     SerialUSB.print(" pos="); SerialUSB.print(currentLowPos[i]);
     SerialUSB.print(" val="); SerialUSB.print(currentLowVals[i]);
     SerialUSB.print(" status=");
     SerialUSB.println(lowPeakLost[i] ? "LOST" : "OK");
   }
 }
 SerialUSB.println("[END] peak_tracking");
}

void spectralPurityCurve(float startmA, float stopmA, float stepuA){
  
  int step_slower = round(stepuA/(20*0.055));
  // int step_xbeam = round(stepuA/(100*0.055));
  int step_xbeam = round(stepuA/(20*0.055));

  int start_slower = round(startmA*50/0.055);
  int stop_slower = round(stopmA*50/0.055);

  // int start_xbeam = round(startmA*10/0.055);
  // int stop_xbeam = round(stopmA*10/0.055);
  int start_xbeam = round(startmA*50/0.055);
  int stop_xbeam = round(stopmA*50/0.055);

  int nsteps = min((stop_slower-start_slower) / step_slower, 512);
  // int nsteps_xbeam = min((stop_xbeam-start_xbeam) / step_xbeam, 512);

  // Absolute DAC code corresponding to step index 0. The sweeps below do not modify
  // slowerADCout/xbeamADCout, so these stay valid for the whole routine and are the
  // bridge between the relative steps[] array and absolute DAC codes.
  const int slower_base = slowerADCout + start_slower;
  const int xbeam_base  = xbeamADCout  + start_xbeam;

  int slower_steps[nsteps];
  int xbeam_steps[nsteps];

  uint16_t slower_peaks_up[nsteps];
  uint16_t xbeam_peaks_up[nsteps];

  uint16_t slower_peaks_down[nsteps];
  uint16_t xbeam_peaks_down[nsteps];

  for (int i = 0; i < nsteps; i++){
    int slower_out = slower_base + i*step_slower;
    analogWrite(SLOWER_DAC, slower_out);
    delay(100);
    trackPeaksInRegions();
    uint16_t slower_peaks_mean = 0.0;
    int n_slower_peaks = 0;
    for (int j = 0; j < MAX_CLUSTERS; j++) {
      uint16_t peakval = currentHighVals[j];
      if (peakval > 0){
        slower_peaks_mean += peakval;
        n_slower_peaks ++;
      }
    }
    slower_peaks_up[i] = slower_peaks_mean / n_slower_peaks;
    slower_steps[i] = i*step_slower;

  }

  for (int i = nsteps-1; i >= 0; i--){
    int slower_out = slower_base + i*step_slower;
    analogWrite(SLOWER_DAC, slower_out);
    delay(100);
    trackPeaksInRegions();
    uint16_t slower_peaks_mean = 0.0;
    int n_slower_peaks = 0;
    for (int j = 0; j < MAX_CLUSTERS; j++) {
      uint16_t peakval = currentHighVals[j];
      if (peakval > 0){
        slower_peaks_mean += peakval;
        n_slower_peaks ++;
      }
    }
    slower_peaks_down[i] = slower_peaks_mean / n_slower_peaks;

  }  

  analogWrite(SLOWER_DAC, slowerADCout);
  delay(2000);

  for (int i = 0; i < nsteps; i++){
    int xbeam_out = xbeam_base + i*step_xbeam;
    analogWrite(XBEAM_DAC, xbeam_out);
    delay(100);
    trackPeaksInRegions();
    uint16_t xbeam_peaks_mean = 0.0;
    int n_xbeam_peaks = 0;
    for (int j = 0; j < MAX_CLUSTERS; j++) {
      uint16_t peakval = currentLowVals[j];
      if (peakval > 0){
        xbeam_peaks_mean += peakval;
        n_xbeam_peaks ++;
      }
    }
    xbeam_peaks_up[i] = xbeam_peaks_mean / n_xbeam_peaks;
    xbeam_steps[i] = i*step_xbeam;

  }

  for (int i = nsteps-1; i >= 0; i--){
    int xbeam_out = xbeam_base + i*step_xbeam;
    analogWrite(XBEAM_DAC, xbeam_out);
    delay(100);
    trackPeaksInRegions();
    uint16_t xbeam_peaks_mean = 0.0;
    int n_xbeam_peaks = 0;
    for (int j = 0; j < MAX_CLUSTERS; j++) {
      uint16_t peakval = currentLowVals[j];
      if (peakval > 0){
        xbeam_peaks_mean += peakval;
        n_xbeam_peaks ++;
      }
    }
    xbeam_peaks_down[i] = xbeam_peaks_mean / n_xbeam_peaks;

  }  

  analogWrite(XBEAM_DAC, xbeamADCout);

  slowerFbParams = getSlope(0, 0.5, nsteps, slower_base, slower_steps, slower_peaks_down);
  xbeamFbParams = getSlope(1, 0.5, nsteps, xbeam_base, xbeam_steps, xbeam_peaks_down);

  // x0 is now an absolute DAC code, so this assignment is finally dimensionally correct.
  slowerGoodADC = slowerFbParams.x0;
  xbeamGoodADC = xbeamFbParams.x0;
  SPC_init = true;
  
  SerialUSB.println("[START] Spectral Purity Curve");

  SerialUSB.println("BEGIN_Slower");
  SerialUSB.print("Slower size = "); SerialUSB.println(nsteps);
  
  for (int i = 0; i < nsteps; i++) {
      SerialUSB.print(slower_steps[i]); SerialUSB.print(","); 
  }
  SerialUSB.println();
  
  for (int i = 0; i < nsteps; i++) {
      SerialUSB.print(slower_peaks_up[i]); SerialUSB.print(",");
  }
  SerialUSB.println();
  
  for (int i = 0; i < nsteps; i++) {
      SerialUSB.print(slower_peaks_down[i]); SerialUSB.print(",");
  }
  SerialUSB.println();
  
  SerialUSB.println("END_Slower");


  SerialUSB.println("BEGIN_Xbeam");

  SerialUSB.print("Xbeam size = "); SerialUSB.println(nsteps);

  for (int i = 0; i < nsteps; i++) {
      SerialUSB.print(xbeam_steps[i]); SerialUSB.print(","); 
  }
  SerialUSB.println(); // end line
  
  for (int i = 0; i < nsteps; i++) {
      SerialUSB.print(xbeam_peaks_up[i]); SerialUSB.print(",");
  }
  SerialUSB.println();

  for (int i = 0; i < nsteps; i++) {
      SerialUSB.print(xbeam_peaks_down[i]); SerialUSB.print(",");
  }
  SerialUSB.println();
  
  SerialUSB.println("END Xbeam");

  // WIRE-FORMAT COMPATIBILITY: x0 is stored internally as an absolute DAC code, but the
  // host's read_purity_curve() plots it against the relative steps[] axis, so convert
  // back to an offset here. Remove this conversion when the framed binary protocol lands
  // and the host can be updated in step.
  SerialUSB.println("START Slopes");
  SerialUSB.print(slowerFbParams.m);SerialUSB.print(",");SerialUSB.print(slowerFbParams.x0 - slower_base);SerialUSB.print(",");SerialUSB.println(slowerFbParams.y0);
  SerialUSB.print(xbeamFbParams.m);SerialUSB.print(",");SerialUSB.print(xbeamFbParams.x0 - xbeam_base);SerialUSB.print(",");SerialUSB.println(xbeamFbParams.y0);
  SerialUSB.println("END Slopes");
  SerialUSB.println("[END] Spectral Purity Curve");

  delay(1000);
  
}

// Returns the local slope of the peak-height vs DAC-code curve, plus the location and
// height of the maximum. NOTE: x0 is an ABSOLUTE DAC code, not an offset into steps[].
// Callers pass baseCode = the absolute DAC code at step index 0.
Slope getSlope(int laser, float thresh, int nsteps, int baseCode,
               const int* steps, const uint16_t* peaks){
  // --- 1. Find the peak ---
  uint16_t maxVal = 0;
  int maxInd = 0;
  for (int i = 0; i < nsteps; i++) {
    if (peaks[i] > maxVal) {
      maxVal = peaks[i];
      maxInd = i;
    }
  }

  // --- 2. Estimate background (average of edges) ---
  const int nbackgroundsteps = min(10, nsteps / 2);
  uint32_t background = 0;
  int count = 0;
  for (int i = 0; i < nbackgroundsteps; i++) {
    background += peaks[i] + peaks[nsteps - 1 - i];
    count += 2;
  }
  if (count > 0)
    background /= count;
  else
    background = 0;

  // --- 3. Determine feedback direction ---
  int fb_sign = 0;
  if (laser == 0) fb_sign = slower_fb_sign;
  else if (laser == 1) fb_sign = xbeam_fb_sign;
  else fb_sign = 1; // default positive direction

  if (fb_sign == 0) {
    Slope s = {0.0f, baseCode + steps[maxInd], maxVal};
    return s;
  }

  // --- 4. Find where peak drops below threshold ---
  float threshLevel = background + thresh * (float)(maxVal - background);

  int i = maxInd;
  int threshInd = maxInd;
  uint16_t threshVal = maxVal;

  while (true) {
    i += fb_sign;
    if (i <= 0 || i >= nsteps - 1) break; // stay within bounds
    if (peaks[i] <= threshLevel) {
      threshInd = i;
      threshVal = peaks[i];
      break;
    }
  }

  // --- 5. Compute slope (Δy / Δx) ---
  float m = 0.0;
  int dx = steps[maxInd] - steps[threshInd];
  if (dx != 0) {
    m = ((float)(maxVal - threshVal)) / (float)dx;
  }

  // dx == 0 means no threshold crossing was found: the peak sits within one step of the
  // end of the sweep in the fb_sign direction, so the search loop broke on the bounds
  // check before assigning threshInd. Zero the width rather than leaving a stale value
  // from a previous SPC run. This is the same behaviour the missing braces used to give,
  // now stated deliberately.
  const int width = (dx != 0) ? abs(dx) : 0;
  if (laser == 0) { slowerFWHM = width; }
  if (laser == 1) { xbeamFWHM  = width; }

  if (dx == 0) {
    // Unconditional (not gated on `debug`): this is a fault, not a trace message.
    SerialUSB.print("[WARN] getSlope: no threshold crossing found for laser ");
    SerialUSB.print(laser);
    SerialUSB.println(". Slope and FWHM set to 0; feedback will arm with a degenerate fit.");
  }

  Slope slope = {m, baseCode + steps[maxInd], maxVal};
  return slope;
}

bool check_Digilock(){
  // returns true if digilock good
  return digitalRead(DIGILOCK_STATUS);
}

// Central DAC write. Every position change goes through here so the software copy and the
// hardware can never disagree, and the rail check exists in exactly one place. Returns
// false if the requested code hit a rail, in which case the channel is parked at midscale
// and flagged at-limit -- matching the behaviour the old per-channel code had inline.
bool setDac(Channel &ch, int code) {
  int c = constrain(code, 0, 4095);
  if ((c == 0) || (c == 4095)) {
    ch.atLimit = true;
    ch.dacCode = 2048;
    analogWrite(ch.dacPin, 2048);
    if (debug) {SerialUSB.print("[DEBUG] ");SerialUSB.print(ch.name);SerialUSB.println(" at limit! Parked at midscale.");}
    return false;
  }
  ch.dacCode = c;
  analogWrite(ch.dacPin, c);
  return true;
}

float getIterationMean(Channel &ch){
  RunningBuffer iterAvg(AVG_BUFFER_SIZE); // do some averaging
  for (int j = 0; j < AVG_BUFFER_SIZE; j++){
    trackPeaksInRegions();
    iterAvg.push(ch.buf->latest());
  }
  return iterAvg.getMean();
}

// Climb toward the peak in small steps. Most common failure mode is a slow drift off the
// top of the plateau, which this walks back.
void bumpUp(Channel &ch){
  int nbumps = 0;
  bool success = false;

  if (debug) {SerialUSB.print("[DEBUG] bump up called for ");SerialUSB.println(ch.name);}

  // Digilock checked BEFORE any DAC movement. The slower channel did this; the xbeam
  // wrote its probe first and only then checked.
  if (!check_Digilock()){return;}

  float start_val = ch.buf->getMean();
  const int step = max(1, (int)round(bump_thresh * ch.fwhm));

  // === Probe for slope direction ===
  // Without this the staircase assumes the peak is always in the +1 direction, which is
  // how a channel fails straight into relock whenever drift goes the other way. Probe
  // only: deliberately does not commit ch.dacCode, since we return to the start below.
  int probe = constrain(ch.dacCode + step, 0, 4095);
  analogWrite(ch.dacPin, probe);
  delay(10);

  if (!check_Digilock()){ analogWrite(ch.dacPin, ch.dacCode); return; } // restore on bail

  float test_val = getIterationMean(ch);

  int effective_sign = 1;
  if (test_val < start_val) {
    effective_sign = -1; // invert bumping direction
    if (debug) {SerialUSB.print("[DEBUG] ");SerialUSB.print(ch.name);SerialUSB.println(" slope reversed (negative slope detected)");}
  } else {
    if (debug) {SerialUSB.print("[DEBUG] ");SerialUSB.print(ch.name);SerialUSB.println(" slope normal (positive slope detected)");}
  }

  analogWrite(ch.dacPin, ch.dacCode); // return to starting point
  delay(10);

  // === Main bump loop ===
  while (nbumps < 10) {
    if (!setDac(ch, ch.dacCode + effective_sign * step)) return;
    delay(10);

    if (!check_Digilock()){return;}

    float iteration_mean = getIterationMean(ch);

    if (iteration_mean > ch.losingThresh * ch.refHeight) { // success
      success = true;
      digitalWrite(ch.statusPin, HIGH);
      ch.goodCode = ch.dacCode;
      if (debug) {SerialUSB.print("[DEBUG] bump up successful on iteration ");SerialUSB.println(nbumps);}
      applySafeBias(ch);
      break;
    }

    else if (iteration_mean < lost_meanThresh * ch.refHeight) { // fell off the peak entirely
      // Tested BEFORE the passed-maximum branch. In the other order this was provably
      // unreachable: start_val is always >= lost_meanThresh*refHeight (serviceChannel
      // gates entry on it, and later passes set it to a reading that cleared this test),
      // so anything low enough to land here is also below start_val.
      //
      // Land one FWHM out on the gentle (fbSign) flank. That is the only side relock can
      // approach the peak from, since it only travels in the -fbSign direction.
      if (!setDac(ch, ch.goodCode + ch.fbSign * ch.fwhm)) return;
      delay(10);
      if (debug) {SerialUSB.println("[DEBUG] bump up resulted in LOST state. Jumping to the gentle flank and passing to relock.");}
      break;
    }

    else if (iteration_mean < start_val) { // passed the maximum
      if (debug) {SerialUSB.println("[DEBUG] Negative slope detected. Passing to relock.");}
      if (effective_sign != ch.fbSign) {
        // Overshot onto the sharp flank. Step back against the direction of travel, far
        // enough to cross the peak onto the fbSign side. When effective_sign == fbSign
        // the overshoot already left us on the gentle side and no retreat is wanted --
        // retreating there would push us back across onto the sharp side.
        if (!setDac(ch, (int)round(ch.dacCode - effective_sign * 0.25 * ch.fwhm))) return;
        delay(10);
      }
      break;
    }

    nbumps++;
    start_val = iteration_mean;
  }

  if (!success){
    relock(ch, 0);
  }
}

// Linear extrapolation back to the setpoint using the SPC slope. Assumes the peak height
// is in the "losing" but not "lost" region.
void relock(Channel &ch, int nattempts){

  // Count only the top-level entry, not the recursive retries.
  if (nattempts == 0) recordInstabilityEvent(ch);

  if (nattempts > 5){
    if (debug) {SerialUSB.println("[DEBUG] Max relock attempts reached!");}
    digitalWrite(ch.statusPin, LOW);
    ch.fail = true;
    return;
  }

  if (!check_Digilock()){return;}

  if (debug) {SerialUSB.print("[DEBUG] Relock called for ");SerialUSB.print(ch.name);SerialUSB.print(", nattempts: ");SerialUSB.println(nattempts);}

  float start_peak_val = getIterationMean(ch);

  // Guard the degenerate fit BEFORE dividing. m == 0 (or non-finite) means the SPC gave
  // no usable slope, so there is no correction worth computing. A tiny-but-nonzero m is
  // handled by the step clamp below, so an exact comparison suffices here.
  if ((ch.fb.m == 0.0f) || !isfinite(ch.fb.m) || (ch.fwhm <= 0)) {
    SerialUSB.print("[WARN] relock: degenerate SPC fit (m == 0 or FWHM == 0) for ");
    SerialUSB.print(ch.name);
    SerialUSB.println(". Passing to recovery.");
    digitalWrite(ch.statusPin, LOW);
    ch.fail = true;
    return;
  }

  // Target is the init-time reference, NOT ch.fb.y0. The SPC contributes only the slope.
  int dx = round(relock_thresh * (ch.refHeight - start_peak_val) / ch.fb.m);

  // Limit one correction to 2 FWHM. The fit is linear but the plateau is not, so
  // extrapolating from far down the flank can overshoot the plateau entirely.
  const int dxMax = 2 * ch.fwhm;
  if (dx >  dxMax) { dx =  dxMax; if (debug) {SerialUSB.println("[DEBUG] relock: dx clamped to +2 FWHM");} }
  if (dx < -dxMax) { dx = -dxMax; if (debug) {SerialUSB.println("[DEBUG] relock: dx clamped to -2 FWHM");} }

  if (debug) {SerialUSB.print("[DEBUG] calculated bump (dx): "); SerialUSB.println(dx);}

  // A correction is only accepted in the direction opposite to fbSign. This is the single
  // generalisation of "only bump down the slower" and "only bump UP for negative sign".
  if (dx * ch.fbSign < 0) {
    ch.dacCode = constrain(ch.dacCode + dx, 0, 4095); // clamp: dx is an extrapolation
    analogWrite(ch.dacPin, ch.dacCode);
  }
  else {
    if (debug){
      SerialUSB.print("[DEBUG] dx in the wrong direction. Exiting relock for ");SerialUSB.print(ch.name);
      SerialUSB.print(". start peak val: ");SerialUSB.print(start_peak_val);
      SerialUSB.print(", ref/m: ");SerialUSB.print(ch.refHeight);SerialUSB.print("/");SerialUSB.println(ch.fb.m);
    }
    return; // something is wrong if it wants to bump the wrong way. let the next call handle it.
  }

  if (!check_Digilock()){return;}
  float latest_peak = getIterationMean(ch);

  if ((latest_peak < ch.losingThresh * ch.refHeight) && (latest_peak > start_peak_val)){
    delay(10);
    relock(ch, nattempts + 1);
  }
  else if (latest_peak > ch.losingThresh * ch.refHeight){ // success
    if (debug) {SerialUSB.print("[DEBUG] Relock successful on iteration ");SerialUSB.println(nattempts);}
    digitalWrite(ch.statusPin, HIGH);
    for (int i = 0; i < BUFFER_SIZE; i++) {
      ch.buf->push((uint16_t) latest_peak); // restart buffer
    }
    ch.goodCode = ch.dacCode;
    applySafeBias(ch);
    return;
  }
  else if (latest_peak < lost_meanThresh * ch.refHeight){
    if (debug) {SerialUSB.println("[DEBUG] Relock failed. Passing to recovery on next loop.");}
    ch.fail = true;
    digitalWrite(ch.statusPin, LOW);
    return;
  }
}

// Full-range sweep looking for the peak after the lock has been lost outright.
bool recover(Channel &ch){
  ch.nRecoveryAttempts++;
  if (ch.nRecoveryAttempts > NRECOVERY){
    if (debug) {SerialUSB.println("[DEBUG] Max recovery attempts reached! Exiting.");}
    digitalWrite(ch.failurePin, HIGH);
    return false;
  }

  if (!check_Digilock()){return false;}

  if (debug) {SerialUSB.print("[DEBUG] Recover called for ");SerialUSB.println(ch.name);}

  if (ch.fwhm <= 0) {
    SerialUSB.print("[WARN] recover: FWHM is zero (no valid SPC fit) for ");
    SerialUSB.print(ch.name);
    SerialUSB.println(". Cannot search.");
    digitalWrite(ch.failurePin, HIGH);
    return false;
  }

  int stepuA = 30;
  int step = max(1, (int)round(stepuA/(20*0.055)));
  const int sweepDir = -ch.fbSign; // slower sweeps down, xbeam sweeps up

  ch.dacCode = constrain(round(ch.goodCode + ch.fbSign*(ch.nRecoveryAttempts+1)*ch.fwhm), 0, 4095);
  analogWrite(ch.dacPin, ch.dacCode);
  delay(50);

  bool success = false;
  bool hitRail = false;
  uint16_t maxVal = 0;

  while ((sweepDir < 0) ? (ch.dacCode > 0) : (ch.dacCode < 4095)) {
    // Clamp only at the rail. The original loops tested the bound before stepping and
    // wrote the result unclamped, so the step could overshoot and wrap through
    // analogWrite's 12-bit mask to the opposite end of the range.
    int next = ch.dacCode + sweepDir * step;
    if (next <= 0)    { next = 0;    hitRail = true; }
    if (next >= 4095) { next = 4095; hitRail = true; }
    ch.dacCode = next;
    analogWrite(ch.dacPin, ch.dacCode);
    delay(50);

    if (!check_Digilock()){return false;}
    trackPeaksInRegions();

    uint16_t current = (uint16_t)getIterationMean(ch);
    if (current > maxVal){
      maxVal = current;
    }

    if ((float)maxVal > ch.losingThresh * ch.refHeight){
      if (debug) {SerialUSB.println("[DEBUG] Peak found during recovery. Breaking.");}
      success = true;
      break;
    }

    if (hitRail) break;
  }

  if (success) {
    ch.fail = false;
    ch.atLimit = false;
    digitalWrite(ch.statusPin, HIGH);
    digitalWrite(ch.failurePin, LOW);
    ch.nRecoveryAttempts = 0;
    for (int i = 0; i < BUFFER_SIZE; i++) { // restart buffer
      ch.buf->push(maxVal);
    }
    ch.goodCode = ch.dacCode; // save last good ADC value
    applySafeBias(ch);
    return true;
  }
  else {
    if (hitRail) {
      // Swept all the way to the rail without finding the peak. Flag it and park the DAC
      // back at the last known-good code rather than leaving it at the rail.
      ch.atLimit = true;
      digitalWrite(ch.failurePin, HIGH);
      ch.dacCode = constrain(ch.goodCode, 0, 4095);
      analogWrite(ch.dacPin, ch.dacCode);
      SerialUSB.print("[WARN] recover: swept to the DAC rail without finding the peak for ");
      SerialUSB.print(ch.name);
      SerialUSB.println(". At limit; reverted to last good output.");
    }
    if (debug) {SerialUSB.println("[DEBUG] Recovery failed.");}
    return false;
  }
}

// One feedback iteration for one channel.
void serviceChannel(Channel &ch) {

  if (!check_Digilock()){return;}

  float running_mean = ch.buf->getMean();
  float running_std  = ch.buf->getStd();

  if (ch.fail) {
    if (recover(ch)) {
      digitalWrite(ch.failurePin, LOW);
      digitalWrite(ch.statusPin, HIGH);
    }
    return;
  }

  if (digitalRead(ch.enablePin) &&
      ((running_mean < ch.losingThresh * ch.refHeight) ||
       (running_std  > losing_stdThresh  * ch.refStd))) { // below threshold, or bouncing

    if (debug) {SerialUSB.print("[DEBUG] Losing ");SerialUSB.print(ch.name);SerialUSB.println(" detected");}

    if (running_mean < lost_meanThresh * ch.refHeight) { // lost
      if (debug) {SerialUSB.print("[DEBUG] Lost ");SerialUSB.print(ch.name);SerialUSB.println(" detected");}
      recordInstabilityEvent(ch);
      digitalWrite(ch.statusPin, LOW);

      if (!setDac(ch, (int)round(ch.dacCode + ch.fbSign * ch.fwhm))) return; // bump by FWHM
      delay(50);
      trackPeaksInRegions(); // resample

      if ((float)ch.buf->latest() > lost_meanThresh * ch.refHeight) {
        relock(ch, 0); // bump landed on the safe tail
      }
      else {
        if (debug) {SerialUSB.println("[DEBUG] Bump 1 failed. Bumping other way.");}
        if (!setDac(ch, (int)round(ch.dacCode - 2 * ch.fbSign * ch.fwhm))) return;
        delay(50);
        trackPeaksInRegions();
        if ((float)ch.buf->latest() > lost_meanThresh * ch.refHeight) {
          relock(ch, 0);
        }
        else {
          ch.fail = true;
          if (debug) {SerialUSB.println("[DEBUG] Bump 2 failed. Exiting.");}
        }
      }
    }
    else {
      bumpUp(ch); // both channels now follow the flowchart
    }
  }
  else if (digitalRead(ch.enablePin)) {
    // Locked and enabled: optionally creep toward the gentle flank.
    continuousBump(ch);
  }

  backoffTick(ch); // slow creep back toward nominal, rate-limited internally
}

void feedbackWrapper() {
  serviceChannel(slower);
  serviceChannel(xbeam);
}

// ---------- Holdoff drift servo ----------

// Mean position error over all tracked clusters on both channels, averaged over nscans.
// Returns false if no cluster stayed trackable, which is how the calibration sweep detects
// that it has pushed the peaks out of their regions.
bool measureHoldoffError(int nscans, float &out) {
  holdoffErrSum = 0.0;
  holdoffErrN   = 0;
  for (int k = 0; k < nscans; k++) {
    if (!trackPeaksInRegions()) return false;
  }
  if (holdoffErrN == 0) return false;
  out = (float)(holdoffErrSum / (double)holdoffErrN);
  holdoffErrSum = 0.0;
  holdoffErrN   = 0;
  return true;
}

// Adaptive calibration. Steps the holdoff outward in holdoff_cal_step increments until the
// peaks have moved by holdoff_cal_frac * SEARCH_WINDOW, which fixes the number of steps
// without needing the slope in advance, then sweeps symmetrically about the starting point
// so the least-squares fit is balanced rather than one-sided.
bool calibrateHoldoff() {
  if (!initialized) {
    SerialUSB.println("[WARN] holdoff calibration: not initialized, no regions to track.");
    return false;
  }

  const int d0 = delayus;
  const float target = holdoff_cal_frac * (float)SEARCH_WINDOW;
  float e0 = 0.0f;

  if (!measureHoldoffError(HOLDOFF_CAL_SCANS, e0)) {
    SerialUSB.println("[WARN] holdoff calibration: could not measure a baseline.");
    return false;
  }

  // --- probe outward to find how many steps span the target displacement ---
  int nsteps = 0;
  for (int k = 1; k <= HOLDOFF_CAL_MAX_STEPS; k++) {
    int d = constrain(d0 + k * holdoff_cal_step, holdoff_min, holdoff_max);
    if (d == delayus) break;              // clamped, cannot go further
    delayus = d;
    float e;
    if (!measureHoldoffError(HOLDOFF_CAL_SCANS, e)) break;  // peaks left their regions
    nsteps = k;
    if (fabs(e - e0) >= target) break;
  }
  delayus = d0;

  if (nsteps < 1) {
    delayus = d0;
    SerialUSB.println("[WARN] holdoff calibration: no usable displacement. Increase holdoff_cal_step.");
    return false;
  }

  // --- symmetric sweep and least-squares fit of error against holdoff ---
  double sx = 0, sy = 0, sxx = 0, sxy = 0;
  int n = 0;
  SerialUSB.println("[START] Holdoff Calibration");
  SerialUSB.print("step_us,");   SerialUSB.println(holdoff_cal_step);
  SerialUSB.print("nsteps,");    SerialUSB.println(nsteps);
  SerialUSB.println("START Points");

  for (int k = -nsteps; k <= nsteps; k++) {
    int d = constrain(d0 + k * holdoff_cal_step, holdoff_min, holdoff_max);
    delayus = d;
    float e;
    if (!measureHoldoffError(HOLDOFF_CAL_SCANS, e)) continue;
    SerialUSB.print(d); SerialUSB.print(","); SerialUSB.println(e, 3);
    sx += d; sy += e; sxx += (double)d*d; sxy += (double)d*e;
    n++;
  }
  SerialUSB.println("END Points");

  delayus = d0;  // always restore

  if (n < 3) {
    SerialUSB.println("slope,0");
    SerialUSB.println("[END] Holdoff Calibration");
    SerialUSB.println("[WARN] holdoff calibration: too few usable points.");
    return false;
  }

  double denom = (n * sxx) - (sx * sx);
  if (denom == 0.0) {
    SerialUSB.println("slope,0");
    SerialUSB.println("[END] Holdoff Calibration");
    SerialUSB.println("[WARN] holdoff calibration: degenerate fit.");
    return false;
  }

  float slope = (float)(((n * sxy) - (sx * sy)) / denom); // samples per us, expected negative
  SerialUSB.print("slope,"); SerialUSB.println(slope, 5);
  SerialUSB.println("[END] Holdoff Calibration");

  if (slope == 0.0f || !isfinite(slope)) {
    SerialUSB.println("[WARN] holdoff calibration: slope is zero, not stored.");
    return false;
  }

  holdoff_slope = slope;
  return true;
}

// Slow correction. Consumes the error accumulated by trackPeaksInRegions since the last
// call, so it costs no extra acquisitions.
void holdoffServo() {
  if (!initialized) return;

  static unsigned long lastHoldoffMs = 0;
  unsigned long now = millis();
  if ((now - lastHoldoffMs) < holdoff_interval_ms) return;
  lastHoldoffMs = now;

  // Drain on every interval regardless of whether the loop is enabled. Returning early
  // without draining would let holdoffErrN grow for the whole uptime (~24 days to overflow
  // a 32-bit long at 50 Hz) and would make the reported error a lifetime average.
  if (holdoffErrN < 10) { holdoffErrSum = 0.0; holdoffErrN = 0; return; }
  float err = (float)(holdoffErrSum / (double)holdoffErrN);
  holdoffErrSum = 0.0;
  holdoffErrN   = 0;
  lastHoldoffErr = err;
  lastHoldoffErrValid = true;

  if (!holdoff_fb_enabled || (holdoff_slope == 0.0f)) return;
  // Suppressed while a channel is failed or recovering: those move the DAC, and the peaks
  // with it, so the position error is not drift.
  if (slower.fail || xbeam.fail) return;

  if (fabs(err) < holdoff_actuate_frac * (float)SEARCH_WINDOW) return;

  int correction = (int)round(-err / holdoff_slope);
  correction = constrain(correction, -holdoff_max_step, holdoff_max_step);
  int newDelay = constrain(delayus + correction, holdoff_min, holdoff_max);

  SerialUSB.print("[HOLDOFF] err=");   SerialUSB.print(err, 2);
  SerialUSB.print(" samples, delayus "); SerialUSB.print(delayus);
  SerialUSB.print(" -> ");             SerialUSB.println(newDelay);

  delayus = newDelay;
}

void reportHoldoff() {
  SerialUSB.println("[START] Holdoff Status");
  SerialUSB.print("enabled,");    SerialUSB.println(holdoff_fb_enabled ? 1 : 0);
  SerialUSB.print("slope,");      SerialUSB.println(holdoff_slope, 5);
  SerialUSB.print("delayus,");    SerialUSB.println(delayus);
  SerialUSB.print("cal_step,");   SerialUSB.println(holdoff_cal_step);
  SerialUSB.print("deadband,");   SerialUSB.println(holdoff_actuate_frac * (float)SEARCH_WINDOW, 2);
  SerialUSB.print("error,");
  if (lastHoldoffErrValid) SerialUSB.println(lastHoldoffErr, 3);
  else                     SerialUSB.println("nan");
  SerialUSB.println("[END] Holdoff Status");
}

// ---------- Safe-side bias ----------

// Deliberately park off-peak, on the gentle flank. Called after each successful
// acquisition. Verifies the offset did not cost too much height and reverts if it did, so
// a wrong fwhm cannot walk the channel off the plateau.
void applySafeBias(Channel &ch) {
  if (!ch.biasEnabled || (ch.fwhm <= 0)) return;

  int off = ch.fbSign * (int)round(bias_frac * ch.fwhm);
  if (off == 0) return;

  const int before = ch.dacCode;
  if (!setDac(ch, ch.dacCode + off)) return;
  delay(10);

  if (!trackPeaksInRegions()) { setDac(ch, before); return; }

  if ((float)ch.buf->latest() < ch.losingThresh * ch.refHeight) {
    setDac(ch, before);  // offset cost too much height, stay where we were
    if (debug) {SerialUSB.print("[DEBUG] safe bias reverted for ");SerialUSB.println(ch.name);}
  } else {
    ch.goodCode = ch.dacCode;
    if (debug) {SerialUSB.print("[DEBUG] safe bias applied for ");SerialUSB.print(ch.name);SerialUSB.print(", offset ");SerialUSB.println(off);}
  }
}

// Try one safe-side step per locked iteration and keep it if the height holds up. Biases
// the operating point away from the sharp edge without waiting for a loss. Costs one extra
// scan per iteration, so it is off by default.
void continuousBump(Channel &ch) {
  if (!ch.contBumpEnabled || (ch.fwhm <= 0)) return;

  if (ch.contBumpSkip > 0) { ch.contBumpSkip--; return; }

  const int step = max(1, (int)round(bump_thresh * ch.fwhm));
  const int before = ch.dacCode;

  if (!setDac(ch, ch.dacCode + ch.fbSign * step)) return;
  delay(10);

  if (!trackPeaksInRegions()) { setDac(ch, before); return; }

  if ((float)ch.buf->latest() < ch.losingThresh * ch.refHeight) {
    setDac(ch, before);
    // Hold off before retrying, otherwise the channel dithers by one step every iteration
    // once it reaches the edge of the acceptable band.
    ch.contBumpSkip = 10;
  } else {
    ch.goodCode = ch.dacCode;
  }
}

// ---------- Instability back-off ----------

void recordInstabilityEvent(Channel &ch) {
  unsigned long now = millis();
  ch.lastEventMs = now;

  if ((ch.eventWindowStart == 0) || ((now - ch.eventWindowStart) > (unsigned long)instab_window_ms)) {
    ch.eventWindowStart = now;
    ch.eventCount = 1;
    return;
  }
  ch.eventCount++;

  if (ch.eventCount < instab_events) return;

  // Threshold exceeded: back off fast.
  ch.eventCount = 0;
  ch.eventWindowStart = now;

  if (ch.losingThresh <= backoff_floor) {
    if (!ch.floorLatched) {
      ch.floorLatched = true;
      SerialUSB.print("[WARN] ");SerialUSB.print(ch.name);
      SerialUSB.println(" unstable at the back-off floor. Threshold will not drop further; check injection power and alignment.");
    }
    return;
  }

  ch.losingThresh -= backoff_step;
  if (ch.losingThresh < backoff_floor) ch.losingThresh = backoff_floor;

  SerialUSB.print("[WARN] ");SerialUSB.print(ch.name);
  SerialUSB.print(" instability detected, backing off acting threshold to ");
  SerialUSB.println(ch.losingThresh, 3);
}

// Slow creep back toward nominal after a quiet period. Deliberately asymmetric with the
// back-off: react fast, return cautiously, or the threshold oscillates at the instability
// period and becomes its own limit cycle.
void backoffTick(Channel &ch) {
  if (ch.losingThresh >= losing_meanThresh) return;

  unsigned long now = millis();
  if ((now - ch.lastEventMs) < backoff_quiet_ms) return;
  if ((now - ch.lastCreepMs) < backoff_quiet_ms) return;
  ch.lastCreepMs = now;

  ch.losingThresh += backoff_creep;
  if (ch.losingThresh > losing_meanThresh) ch.losingThresh = losing_meanThresh;
  if (ch.losingThresh > backoff_floor) ch.floorLatched = false;

  if (debug) {
    SerialUSB.print("[DEBUG] ");SerialUSB.print(ch.name);
    SerialUSB.print(" quiet, acting threshold creeping back to ");SerialUSB.println(ch.losingThresh, 3);
  }
}

void resetBackoff(Channel &ch) {
  ch.losingThresh      = losing_meanThresh;
  ch.floorLatched      = false;
  ch.eventCount        = 0;
  ch.eventWindowStart  = 0;
  ch.lastEventMs       = 0;
  ch.lastCreepMs       = 0;
  SerialUSB.print("[INFO] back-off reset for ");SerialUSB.print(ch.name);
  SerialUSB.print(", acting threshold restored to ");SerialUSB.println(ch.losingThresh, 3);
}

void reportBackoff() {
  SerialUSB.println("[START] Backoff Status");
  SerialUSB.print("nominal,");SerialUSB.println(losing_meanThresh, 3);
  SerialUSB.print("floor,");  SerialUSB.println(backoff_floor, 3);
  SerialUSB.print("slower,"); SerialUSB.print(slower.losingThresh, 3);
  SerialUSB.print(",");       SerialUSB.print(slower.eventCount);
  SerialUSB.print(",");       SerialUSB.print(slower.floorLatched ? 1 : 0);
  SerialUSB.print(",");       SerialUSB.print(slower.biasEnabled ? 1 : 0);
  SerialUSB.print(",");       SerialUSB.println(slower.contBumpEnabled ? 1 : 0);
  SerialUSB.print("xbeam,");  SerialUSB.print(xbeam.losingThresh, 3);
  SerialUSB.print(",");       SerialUSB.print(xbeam.eventCount);
  SerialUSB.print(",");       SerialUSB.print(xbeam.floorLatched ? 1 : 0);
  SerialUSB.print(",");       SerialUSB.print(xbeam.biasEnabled ? 1 : 0);
  SerialUSB.print(",");       SerialUSB.println(xbeam.contBumpEnabled ? 1 : 0);
  SerialUSB.println("[END] Backoff Status");
}

void parse_change_command(String args){
  // Expects args of syntax "[VARIABLE],[VALUE]"
  int comma1 = args.indexOf(',');
  String var = args.substring(0,comma1);
  String val = args.substring(comma1+1);
  if (var == "unlock_thresh") {losing_meanThresh = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("losing_meanThresh successfully changed to: ");SerialUSB.println(losing_meanThresh);}return;}
  if (var == "lost_thresh") {lost_meanThresh = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("lost_meanThresh successfully changed to: ");SerialUSB.println(lost_meanThresh);}return;}
  if (var == "bump_thresh") {bump_thresh = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("bump_thresh successfully changed to: ");SerialUSB.println(bump_thresh);}return;}
  if (var == "relock_thresh") {relock_thresh = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("relock_thresh successfully changed to: ");SerialUSB.println(relock_thresh);}return;}
  if (var == "std_thresh") {losing_stdThresh = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("losing_stdThresh successfully changed to: ");SerialUSB.println(losing_stdThresh);}return;}
  if (var == "slower_sign") {slower_fb_sign = val.toInt(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("slower_fb_sign successfully changed to: ");SerialUSB.println(slower_fb_sign);}return;}
  if (var == "slower_bias") {slower.biasEnabled = (val.toInt() != 0); reportBackoff(); return;}
  if (var == "xbeam_bias") {xbeam.biasEnabled = (val.toInt() != 0); reportBackoff(); return;}
  if (var == "slower_contbump") {slower.contBumpEnabled = (val.toInt() != 0); slower.contBumpSkip = 0; reportBackoff(); return;}
  if (var == "xbeam_contbump") {xbeam.contBumpEnabled = (val.toInt() != 0); xbeam.contBumpSkip = 0; reportBackoff(); return;}
  if (var == "bias_frac") {bias_frac = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("bias_frac successfully changed to: ");SerialUSB.println(bias_frac);}return;}
  if (var == "instab_window_ms") {instab_window_ms = val.toInt(); return;}
  if (var == "instab_events") {instab_events = val.toInt(); return;}
  if (var == "backoff_step") {backoff_step = val.toFloat(); return;}
  if (var == "backoff_floor") {backoff_floor = val.toFloat(); return;}
  if (var == "backoff_quiet_ms") {backoff_quiet_ms = (unsigned long)val.toInt(); return;}
  if (var == "backoff_creep") {backoff_creep = val.toFloat(); return;}
  if (var == "holdoff_slope") {holdoff_slope = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("holdoff_slope successfully changed to: ");SerialUSB.println(holdoff_slope, 5);}return;}
  if (var == "holdoff_cal_step") {holdoff_cal_step = val.toInt(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("holdoff_cal_step successfully changed to: ");SerialUSB.println(holdoff_cal_step);}return;}
  if (var == "holdoff_cal_frac") {holdoff_cal_frac = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("holdoff_cal_frac successfully changed to: ");SerialUSB.println(holdoff_cal_frac);}return;}
  if (var == "holdoff_actuate_frac") {holdoff_actuate_frac = val.toFloat(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("holdoff_actuate_frac successfully changed to: ");SerialUSB.println(holdoff_actuate_frac);}return;}
  if (var == "holdoff_max_step") {holdoff_max_step = val.toInt(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("holdoff_max_step successfully changed to: ");SerialUSB.println(holdoff_max_step);}return;}
  if (var == "holdoff_interval_ms") {holdoff_interval_ms = (unsigned long)val.toInt(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("holdoff_interval_ms successfully changed to: ");SerialUSB.println(holdoff_interval_ms);}return;}
  if (var == "scan_timeout_ms") {scan_timeout_us = (unsigned long)val.toInt() * 1000UL; if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("scan_timeout_us successfully changed to: ");SerialUSB.println(scan_timeout_us);}return;}
  if (var == "xbeam_sign") {xbeam_fb_sign = val.toInt(); if (debug) {SerialUSB.print("[DEBUG]");SerialUSB.print("xbeam_fb_sign successfully changed to: ");SerialUSB.println(xbeam_fb_sign);}return;}
}

void pinModeSetup(){
  pinMode(ANALOG_PIN, INPUT);
  pinMode(TRIGGER_PIN, INPUT);
  pinMode(DIGILOCK_STATUS, INPUT);
  pinMode(SLOWER_ENABLE, INPUT);
  pinMode(XBEAM_ENABLE, INPUT);
  pinMode(SLOWER_STATUS, OUTPUT);
  pinMode(XBEAM_STATUS, OUTPUT);
  pinMode(SLOWER_FAILURE, OUTPUT);
  pinMode(XBEAM_FAILURE, OUTPUT);

  digitalWrite(SLOWER_STATUS, LOW);
  digitalWrite(XBEAM_STATUS, LOW);
  digitalWrite(SLOWER_FAILURE, LOW);
  digitalWrite(XBEAM_FAILURE, LOW);
}

// ---------- Non-blocking command reception ----------
// readStringUntil() blocked for the Stream timeout (1000 ms) on every pass with no
// command pending, so the servo could not run faster than ~1 Hz when the host was idle
// and only sped up when the dashboard polled. Bytes are now accumulated across loop
// iterations into a fixed buffer and the servo runs every pass.
#define CMD_BUF_LEN 64
static char cmdBuf[CMD_BUF_LEN];
static uint8_t cmdLen = 0;
static bool cmdOverflow = false;

// Returns true when a complete newline-terminated command is sitting in cmdBuf.
bool pollCommand() {
  while (SerialUSB.available() > 0) {
    char c = (char)SerialUSB.read();
    if ((c == '\n') || (c == '\r')) {
      if (cmdLen == 0) continue;             // blank line, or the second half of CRLF
      cmdBuf[cmdLen] = '\0';
      cmdLen = 0;
      if (cmdOverflow) {                     // truncated: drop it rather than misparse
        cmdOverflow = false;
        SerialUSB.println("[WARN] command exceeded buffer; discarded.");
        continue;
      }
      return true;
    }
    if (cmdLen < CMD_BUF_LEN - 1) cmdBuf[cmdLen++] = c;
    else cmdOverflow = true;
  }
  return false;
}

void dispatchCommand(const char* command) {

  if (strcmp(command, "I") == 0) {
    if (debug){Serial.println("Init recieved");}
    initialize_peak_vals_locations();
  }

  else if (strcmp(command, "FB") == 0) {
    feedbackActive = !feedbackActive;
    if (!feedbackActive) { slower_nrecoveryAttempts = 0; xbeam_nrecoveryAttempts = 0; }
  }

  else if (strcmp(command, "ZS") == 0) { // zero the slower output
    slowerADCout = 2048;
    analogWrite(SLOWER_DAC, slowerADCout);
    slower_fail = false;
    slower_at_limit = false;
    slower_nrecoveryAttempts = 0;
    digitalWrite(SLOWER_FAILURE, LOW);
  }

  else if (strcmp(command, "ZX") == 0) { // zero the xbeams output
    xbeamADCout = 2048;
    analogWrite(XBEAM_DAC, xbeamADCout);
    xbeam_fail = false;
    xbeam_at_limit = false;
    xbeam_nrecoveryAttempts = 0;
    digitalWrite(XBEAM_FAILURE, LOW);
  }

  else if (strcmp(command, "Z") == 0) { // zero both outputs
    slowerADCout = 2048;
    analogWrite(SLOWER_DAC, slowerADCout);
    xbeamADCout = 2048;
    analogWrite(XBEAM_DAC, xbeamADCout);
    slower_fail = false;
    xbeam_fail = false;
    slower_at_limit = false;
    xbeam_at_limit = false;
    slower_nrecoveryAttempts = 0;
    xbeam_nrecoveryAttempts = 0;
    initialized = false;
    SPC_init = false;
  }

  else if (strcmp(command, "R") == 0) { // read peaks
    // These were unconditional Serial (hardware UART) writes at 9600 baud, ~4 ms each,
    // immediately before a 1000-byte binary trace. Gated on debug now.
    if (debug) {Serial.println("Recieved command to send trace");}
    sendTrace();
    if (debug) {Serial.println("Sent trace");}
    if (initialized) {printPeakStatus();}
  }

  else if (strcmp(command, "TD") == 0) {
    debug = !debug;
  }

  else if (command[0] == 'B') { // safe-side bias / instability back-off
    if      (strcmp(command, "BR") == 0)  { reportBackoff(); }
    else if (strcmp(command, "BZ") == 0)  { resetBackoff(slower); resetBackoff(xbeam); }
    else if (strcmp(command, "BZS") == 0) { resetBackoff(slower); }
    else if (strcmp(command, "BZX") == 0) { resetBackoff(xbeam); }
    else {
      SerialUSB.print("[WARN] unknown backoff subcommand: ");
      SerialUSB.println(command);
    }
  }

  else if (command[0] == 'H') { // holdoff drift servo
    if (command[1] == 'C') {          // HC: run the adaptive calibration
      calibrateHoldoff();
    }
    else if (command[1] == 'F') {     // HF: toggle the holdoff feedback loop
      holdoff_fb_enabled = !holdoff_fb_enabled;
      if (holdoff_fb_enabled && (holdoff_slope == 0.0f)) {
        SerialUSB.println("[WARN] holdoff feedback enabled but uncalibrated. Run HC, or set Choldoff_slope.");
      }
      reportHoldoff();
    }
    else if (command[1] == 'R') {     // HR: report status
      reportHoldoff();
    }
    else if (command[1] == 'S') {     // HS<value>: set holdoff WITHOUT re-initialising
      delayus = constrain(atoi(command + 2), holdoff_min, holdoff_max);
      reportHoldoff();
    }
    else {
      SerialUSB.print("[WARN] unknown holdoff subcommand: ");
      SerialUSB.println(command);
    }
  }

  else if (command[0] == 'D') { // change the trigger holdoff
    delayus = atoi(command + 1);
    SerialUSB.print("delayus set to: ");
    SerialUSB.println(delayus);
    initialize_peak_vals_locations();
  }

  else if (command[0] == 'S') { // trigger SPC. syntax: "S[startmA],[stopmA],[stepuA]"
    const char* args = command + 1;
    const char* c1 = strchr(args, ',');
    const char* c2 = (c1 != NULL) ? strchr(c1 + 1, ',') : NULL;
    if ((c1 != NULL) && (c2 != NULL)) {
      spectralPurityCurve((float)atof(args), (float)atof(c1 + 1), (float)atof(c2 + 1));
    }
    else {
      spectralPurityCurve(-1.5, 1.5, 25.0);
    }
  }

  else if (command[0] == 'C') { // change a param. syntax: "C[VARIABLE],[VALUE]"
    // Single allocation per C command, which arrives rarely. Not worth rewriting the
    // working parser to avoid.
    parse_change_command(String(command + 1));
  }

  else {
    SerialUSB.print("[WARN] unknown command: ");
    SerialUSB.println(command);
  }
}

// ---------- Setup and loop ----------
void setup() {
 SerialUSB.begin(250000);
 Serial.begin(9600);
 pinModeSetup();
 analogWriteResolution(12);
 analogWrite(SLOWER_DAC, 2048); // slower DAC0
 analogWrite(XBEAM_DAC, 2048); // xbeam DAC1
 setupADC();
}

static uint32_t servoIterations = 0;

void loop() {
 // Servo first. acquireScan() blocks on the scan trigger, so it is the natural clock for
 // this loop and one iteration per scan is the ceiling. Running it ahead of dispatch also
 // means an "R" command returns the buffer acquired this pass, matching the old ordering.
 if ((highClusterMeans[0] > 0) && initialized) {
   if (trackPeaksInRegions()) {
     if (feedbackActive&&SPC_init) {feedbackWrapper();}
     servoIterations++;
   }
 }

 holdoffServo(); // slow, rate-limited internally; consumes accumulated position error

 if (pollCommand()) {
   dispatchCommand(cmdBuf);
 }

 if (trigger_fail) {
   // Rate-limited: with no trigger the loop spins fast and would otherwise flood USB.
   // Feedback is not running (trackPeaksInRegions returned false), so the DAC simply
   // holds its last value, and the command path stays alive so Z still works.
   static uint32_t lastTrigWarnMs = 0;
   uint32_t now = millis();
   if ((now - lastTrigWarnMs) >= 1000) {
     lastTrigWarnMs = now;
     SerialUSB.println("[WARN] scan trigger timeout: no ramp detected. Feedback halted, DAC holding.");
   }
 }

 if (debug) {
  // Rate-limited to ~1 Hz. At the scan rate this block would otherwise saturate the USB
  // buffer and throttle the servo it is reporting on.
  static uint32_t lastDebugMs = 0;
  static uint32_t lastDebugIter = 0;
  uint32_t now = millis();
  uint32_t dt = now - lastDebugMs;
  if (dt >= 1000) {
   uint32_t iters = servoIterations - lastDebugIter;
   lastDebugMs = now;
   lastDebugIter = servoIterations;
   SerialUSB.println();
   SerialUSB.print("[DEBUG]");SerialUSB.print("Servo rate (Hz): ");SerialUSB.println((float)iters * 1000.0f / (float)dt, 1);
   SerialUSB.print("[DEBUG]");SerialUSB.print("Digilock status: ");SerialUSB.println(digitalRead(DIGILOCK_STATUS));
   SerialUSB.print("[DEBUG]");SerialUSB.print("Feedback active: ");SerialUSB.println(feedbackActive);
   SerialUSB.print("[DEBUG]");SerialUSB.print("Slower active: ");SerialUSB.println(digitalRead(SLOWER_ENABLE));
   SerialUSB.print("[DEBUG]");SerialUSB.print("Xbeam active: ");SerialUSB.println(digitalRead(XBEAM_ENABLE));
   SerialUSB.print("[DEBUG]");SerialUSB.print("Slower status: ");SerialUSB.println(digitalRead(SLOWER_STATUS));
   SerialUSB.print("[DEBUG]");SerialUSB.print("Xbeam status: ");SerialUSB.println(digitalRead(XBEAM_STATUS));
   SerialUSB.print("[DEBUG]");SerialUSB.print("Slower failure: ");SerialUSB.println(digitalRead(SLOWER_FAILURE));
   SerialUSB.print("[DEBUG]");SerialUSB.print("Xbeam failure: ");SerialUSB.println(digitalRead(XBEAM_FAILURE));
   SerialUSB.println();
  }
 }
}
