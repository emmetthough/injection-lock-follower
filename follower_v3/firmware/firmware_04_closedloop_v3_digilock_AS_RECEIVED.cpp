/*
 Repeated Peak-Finding Fabry-Perot Scan
 Arduino Due Version
*/
#include <math.h>
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

volatile uint16_t buffer[N];
volatile int adcIndex = 0;
volatile int delayus = 200;

volatile int slowerADCout = 2048;
volatile bool slower_at_limit = false;

volatile int xbeamADCout = 2048;
volatile bool xbeam_at_limit = false;

int slower_fb_sign = 1;
int xbeam_fb_sign = -1;

int slowerFWHM = 0;
int xbeamFWHM = 0;

int slowerGoodADC = 2048;
int xbeamGoodADC = 2048;

bool SPC_init = false;

bool slower_fail = false;
bool xbeam_fail = false;

float losing_meanThresh = 0.95;
float lost_meanThresh = 0.25;
float losing_stdThresh = 2.0;
float relock_thresh = 0.95;
float bump_thresh = 0.05;

int NRECOVERY = 5;
int slower_nrecoveryAttempts = 0;
int xbeam_nrecoveryAttempts = 0;

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

float slowerHeight;
float slowerStd;
float xbeamHeight;
float xbeamStd;

Slope slowerFbParams;
Slope xbeamFbParams;

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
void acquireScan() {
  bool bufferFull = false;
  adcIndex = 0;
  while (!bufferFull){ // repeat until buffer full or timeout
    if (!digitalRead(TRIGGER_PIN)){ // if trigger low
      while (!digitalRead(TRIGGER_PIN)){} // wait until trigger high
      delayMicroseconds(delayus); // holdoff
      while (adcIndex < N){ // fill buffer
        ADC->ADC_CR = ADC_CR_START;
        while (!(ADC->ADC_ISR & ADC_ISR_EOC7));
        buffer[adcIndex++] = ADC->ADC_CDR[7];
      }
      bufferFull = true; // exit loop
    }
  }
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
int clusterPeaks(uint16_t *positions, int count, float *clusterMeans) {
 if (count <= 0) return 0;
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

 while (scanCount < MAX_SCANS) {
   acquireScan();
   addNewPeaks();
   scanCount++;
 }

 highStats = computePeakStats(highPeaks, highPeakPos, foundHigh);
 slowerHeight = highStats.meanHeight;
 slowerStd = highStats.stdHeight;

 lowStats  = computePeakStats(lowPeaks, lowPeakPos, foundLow);
 xbeamHeight = lowStats.meanHeight;
 xbeamStd = lowStats.stdHeight;

 int nHighClusters = clusterPeaks(highPeakPos, foundHigh, highClusterMeans);
 int nLowClusters  = clusterPeaks(lowPeakPos,  foundLow,  lowClusterMeans);

 if (nHighClusters > 0 && nLowClusters > 0) {
   int diff = abs((int)highClusterMeans[0] - (int)lowClusterMeans[0]);
   SEARCH_WINDOW = max(5, diff / 2.5);
 }
 
 initialized = true;
 
 SerialUSB.println("[START] Initalization");
 sendPeaks();
 SerialUSB.println("[END] Peaks");
 SerialUSB.println("[START] Stats");
 sendStats(highStats, lowStats);
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
void trackPeaksInRegions() {
 acquireScan();

 // --- Track high peaks ---
 uint16_t slower_mean = (uint16_t)0;
 int slower_mean_N = 0;
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
   slower_mean += maxVal;
   slower_mean_N ++;
 }

 slower_mean /= slower_mean_N;
 slowerRunningBuffer.push(slower_mean);

 uint16_t xbeam_mean = (uint16_t)0;
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
   xbeam_mean += maxVal;
   xbeam_mean_N ++;
 }
 xbeam_mean /= xbeam_mean_N;
 xbeamRunningBuffer.push(xbeam_mean);
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

  int slower_steps[nsteps];
  int xbeam_steps[nsteps];

  uint16_t slower_peaks_up[nsteps];
  uint16_t xbeam_peaks_up[nsteps];

  uint16_t slower_peaks_down[nsteps];
  uint16_t xbeam_peaks_down[nsteps];

  for (int i = 0; i < nsteps; i++){
    int slower_out = slowerADCout + i*step_slower + start_slower;
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
    int slower_out = slowerADCout + i*step_slower + start_slower;
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
    int xbeam_out = xbeamADCout + i*step_xbeam + start_xbeam;
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
    int xbeam_out = xbeamADCout + i*step_xbeam + start_xbeam;
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

  slowerFbParams = getSlope(0, 0.5, nsteps, slower_steps, slower_peaks_down);
  xbeamFbParams = getSlope(1, 0.5, nsteps, xbeam_steps, xbeam_peaks_down);

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

  SerialUSB.println("START Slopes");
  SerialUSB.print(slowerFbParams.m);SerialUSB.print(",");SerialUSB.print(slowerFbParams.x0);SerialUSB.print(",");SerialUSB.println(slowerFbParams.y0);
  SerialUSB.print(xbeamFbParams.m);SerialUSB.print(",");SerialUSB.print(xbeamFbParams.x0);SerialUSB.print(",");SerialUSB.println(xbeamFbParams.y0);
  Serial.println("END Slopes");
  SerialUSB.println("[END] Spectral Purity Curve");

  delay(1000);
  
}

Slope getSlope(int laser, float thresh, int nsteps,
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
    Slope s = {0.0f, maxInd, maxVal};
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
  if (dx != 0)
    m = ((float)(maxVal - threshVal)) / (float)dx;
    if (laser == 0){slowerFWHM = abs(dx);}
    if (laser == 1){xbeamFWHM = abs(dx);}

  Slope slope = {m, steps[maxInd], maxVal};
  return slope;
}

float get_slower_iteration_mean(){
  
  RunningBuffer iterAvg(AVG_BUFFER_SIZE); // do some averaging
  for (int j = 0; j < AVG_BUFFER_SIZE; j++){
    trackPeaksInRegions();
    iterAvg.push(slowerRunningBuffer.latest());
  }

  return iterAvg.getMean();
}

float get_xbeam_iteration_mean(){
  
  RunningBuffer iterAvg(AVG_BUFFER_SIZE); // do some averaging
  for (int j = 0; j < AVG_BUFFER_SIZE; j++){
    trackPeaksInRegions();
    iterAvg.push(xbeamRunningBuffer.latest());
  }

  return iterAvg.getMean();
}

bool check_Digilock(){
  // returns true if digilock good
  return digitalRead(DIGILOCK_STATUS);
}

void bump_slower_up(){
   // this is to try recovering the lock by small bumping the current up -- most common failure mode
  int nbumps = 0;
  bool success = false;
  float start_val = slowerRunningBuffer.getMean(); // this function is called immediately if unlocking so this val should still be valid.

  Serial.println("bump slower up called!");

  if (!check_Digilock()){return;}
  
  while (nbumps < 10) {
    int bump = constrain(round(slowerADCout + bump_thresh*slowerFWHM), 0, 4095); //try bumping up by 5% of FWHM
    if ((bump == 0)||(bump == 4095)) {slower_at_limit = true; analogWrite(SLOWER_DAC, 2048); return;}
    analogWrite(SLOWER_DAC, bump);
    delay(10);
    
    if (!check_Digilock()){return;}

    float iteration_mean = get_slower_iteration_mean();
    
    if (iteration_mean > losing_meanThresh*slowerHeight){ // if you're successful, break!
      success = true;
      Serial.print("bump slower up successful on iteration ");Serial.println(nbumps);
      slowerGoodADC = bump;
      break;
    } 

    else if (iteration_mean < start_val){ // if you've passed the max, break and pass to relock function
      Serial.print("Negative slope detected. Passing to relock on iteration ");Serial.println(nbumps);
      break;
    }
    
    else if (iteration_mean < lost_meanThresh*slowerHeight) { // if you've bumped too far and fallen off the peak. this should be an extreme edge case.
        bump = constrain(round(slowerADCout - slowerFWHM), 0, 4095); // jump back to safe side, not defined by fb_params since we've been bumping up
        if ((bump == 0)||(bump == 4095)) {slower_at_limit = true; analogWrite(SLOWER_DAC, 2048); return;}
        analogWrite(SLOWER_DAC, bump);
        delay(10);
        Serial.println("bump slower up resulted in LOST state. jumping back to 'safe' side and passing to relock slower.");
        break;
    }
    nbumps++;
    start_val = iteration_mean;
  }

  if (!success){
    relock_slower(0);
  }
  return;
}

void relock_slower(int nattempts){ // this function assumes the peak height is in the "losing" but not "lost" region
  
  if (nattempts > 5){
    Serial.println("Max relock attempts reached!");
    digitalWrite(SLOWER_STATUS, LOW);
    slower_fail = true;
    return;
  }

  if (!check_Digilock()){return;}
  
  Serial.print("Relock slower called! nattempts: ");Serial.println(nattempts);
  
  float start_peak_val = get_slower_iteration_mean();
  int dx = round(relock_thresh*((float)slowerFbParams.y0-start_peak_val)/slowerFbParams.m); // 5% error? check this. relock_thresh
  
  Serial.print("calculated bump (dx): "); Serial.println(dx);
  
  if (dx < 0) { // only bump down the slower. reserve bumps up to the lost state
    slowerADCout += dx;
    analogWrite(SLOWER_DAC, slowerADCout);
  }
  else {
    Serial.println("dx positive. exiting relock slower. debug params listed below.");
    Serial.print("start peak val: ");Serial.print(start_peak_val);Serial.print(", y0/m :");Serial.print(slowerFbParams.y0);Serial.print("/");Serial.println(slowerFbParams.m);
    return;} // something goes wrong if it's trying to bump in the wrong direction. kill it and let the next call take care of it.

  if (!check_Digilock()){return;}
  float latest_peak = get_slower_iteration_mean(); // average

  if ((latest_peak < losing_meanThresh*slowerHeight)&&(latest_peak > start_peak_val)){
    nattempts ++;
    delay(10);
    relock_slower(nattempts);
  }
  else if (latest_peak > losing_meanThresh*slowerHeight){ // success!
    Serial.print("Slower relock successful on iteration ");Serial.println(nattempts);
    for (int i = 0; i < BUFFER_SIZE; i++) {
      slowerRunningBuffer.push((uint16_t) latest_peak); // restart buffer
    }
    slowerGoodADC = slowerADCout; // save last good ADC value
    return;
  } 
  else if (latest_peak < lost_meanThresh*slowerHeight){ // kill FB loop if the relock fails. will try again on next loop.
    Serial.println("Slower relock failed. Passing to recovery on next loop.");
    slower_fail = true;
    digitalWrite(SLOWER_STATUS, LOW);
    return;
  }
}

bool recover_slower(){
  // called if slower failed. bump up 4 FWHM and step down until peak is found again.

  slower_nrecoveryAttempts++;
  if (slower_nrecoveryAttempts > NRECOVERY){
    Serial.println("Max recovery attempts reached! Exiting.");
    digitalWrite(SLOWER_FAILURE, HIGH);
    return false;
  }

  if (!check_Digilock()){return false;}
  
  Serial.println("Recover slower called!");
  
  int stepuA = 30;
  int step_slower = round(stepuA/(20*0.055));
  
  int start_slower = constrain(round(slowerGoodADC + (slower_nrecoveryAttempts+1)*slowerFWHM), 0, 4095); // bump slower up N FWHM
  slowerADCout = start_slower;
  analogWrite(SLOWER_DAC, slowerADCout);

  int i = 0;
  bool success = false;
  uint16_t maxVal = (uint16_t)0.0;
  uint16_t lastVal = (uint16_t)0.0;
  
  while (slowerADCout > 0){
    i++;
    slowerADCout -= step_slower;
    analogWrite(SLOWER_DAC, slowerADCout);
    delay(50);

    if (!check_Digilock()){return false;}
    trackPeaksInRegions();
    
    uint16_t current = (uint16_t)get_slower_iteration_mean();
    if (current > maxVal){
      maxVal = current;
    }

    if ((float)maxVal>losing_meanThresh*slowerHeight){
      Serial.println("Current iteration detected to be within losing thresh. Breaking recovery.");
      success = true;
      break;
    }
    lastVal = current;

  }

  if (success) {
    slower_fail = false;
    digitalWrite(SLOWER_STATUS, HIGH);
    slower_nrecoveryAttempts = 0;
    for (int i = 0; i < BUFFER_SIZE; i++) { // restart buffer
      slowerRunningBuffer.push(maxVal);
    }
    slowerGoodADC = slowerADCout; // save last good ADC value
    return true;
  }
  else {
    Serial.println("Recovery failed.");
    return false;
  }
  
}


void bump_xbeam_up() {
  int nbumps = 0;
  bool success = false;

  Serial.println("bump xbeam up called!");

  // === Determine slope sign on first bump ===
  float start_val = xbeamRunningBuffer.getMean();
  int bump_test = constrain(round(xbeamADCout + bump_thresh * xbeamFWHM), 0, 4095);
  analogWrite(XBEAM_DAC, bump_test);
  delay(10);

  if (!check_Digilock()){return;}

  float test_val = get_xbeam_iteration_mean();

  int effective_sign = 1;  // same as slower by default
  if (test_val < start_val) {
    effective_sign = -1;  // invert bumping direction
    Serial.println("Xbeam slope reversed (negative slope detected)");
  } else {
    Serial.println("Xbeam slope normal (positive slope detected)");
  }

  // Return to starting point before main bump loop
  analogWrite(XBEAM_DAC, xbeamADCout);
  delay(10);

  // === Main bump loop ===
  while (nbumps < 10) {
    int bump = constrain(round(xbeamADCout + effective_sign * bump_thresh * xbeamFWHM), 0, 4095);
    if ((bump == 0) || (bump == 4095)) {xbeam_at_limit = true; analogWrite(XBEAM_DAC, 2048); return;}

    analogWrite(XBEAM_DAC, bump);
    delay(10);

    if (!check_Digilock()){return;}

    float iteration_mean = get_xbeam_iteration_mean();

    if (iteration_mean > losing_meanThresh * xbeamHeight) { // success
      success = true;
      Serial.print("bump xbeam up successful on iteration ");
      Serial.println(nbumps);
      xbeamGoodADC = bump;
      break;
    }

    else if (iteration_mean < start_val) { // passed maximum
      Serial.println("Negative slope detected. Passing to relock.");
      if (effective_sign > 0){ // if you were bumping up, you've passed the peak and are coming down on the sharp side. make a jump back and pass to relock.
        int bump = constrain(round(xbeamADCout - 0.25*xbeamFWHM), 0, 4095);
        if ((bump == 0) || (bump == 4095)) {xbeam_at_limit = true; analogWrite(XBEAM_DAC, 2048); return;}
        analogWrite(XBEAM_DAC, bump);
        delay(10);
      }
      break;
    }

    else if (iteration_mean < lost_meanThresh * xbeamHeight) { // fell off peak
      bump = constrain(round(xbeamADCout - effective_sign * xbeamFWHM), 0, 4095);
      if ((bump == 0) || (bump == 4095)) {
        xbeam_at_limit = true;
        analogWrite(XBEAM_DAC, 2048);
        return;
      }
      analogWrite(XBEAM_DAC, bump);
      delay(10);
      Serial.println("bump xbeam up resulted in LOST state. jumping back and passing to relock_xbeam.");
      break;
    }

    nbumps++;
    start_val = iteration_mean;
  }

  if (!success) {
    relock_xbeam(0);
  }

  return;
}

void relock_xbeam(int nattempts) {
  if (nattempts > 5) {
    Serial.println("Max relock attempts reached!");
    xbeam_fail = true;
    return;
  }

  if (!check_Digilock()){return;}

  Serial.print("Relock xbeam called! nattempts: ");
  Serial.println(nattempts);

  float start_peak_val = get_xbeam_iteration_mean();
  int dx = round(relock_thresh * ((float)xbeamFbParams.y0 - start_peak_val) / xbeamFbParams.m);

  Serial.print("calculated bump (dx): ");
  Serial.println(dx);

  if (dx > 0) {  // only bump UP for negative sign
    xbeamADCout += dx;
    analogWrite(XBEAM_DAC, xbeamADCout);
  } else {
    Serial.println("dx negative. exiting relock xbeam. debug params listed below.");
    Serial.print("start peak val: ");Serial.print(start_peak_val);Serial.print(", y0/m :");Serial.print(xbeamFbParams.y0);Serial.print("/");Serial.println(xbeamFbParams.m);
    
    return;
  }

  if (!check_Digilock()){return;}
  float latest_peak = get_xbeam_iteration_mean();

  if ((latest_peak < losing_meanThresh * xbeamHeight) && (latest_peak > start_peak_val)) {
    nattempts++;
    relock_xbeam(nattempts);
  } else if (latest_peak > losing_meanThresh * xbeamHeight) { // success
    Serial.println("Relock xbeam successful!");
    for (int i = 0; i < BUFFER_SIZE; i++) {
      xbeamRunningBuffer.push((uint16_t)latest_peak);
    }
      xbeamGoodADC = xbeamADCout;
      return;
  } else if (latest_peak < lost_meanThresh * xbeamHeight) {
    Serial.println("Xbeam relock failed. Passing to recovery on next loop.");
    xbeam_fail = true;
    digitalWrite(XBEAM_STATUS, LOW);
    return;
  }
}

bool recover_xbeam() {
  Serial.println("Recover xbeam called!");
  xbeam_nrecoveryAttempts++;
  if (xbeam_nrecoveryAttempts > NRECOVERY){
    Serial.println("Max recovery attempts reached! Exiting.");
    digitalWrite(XBEAM_FAILURE, HIGH);
    return false;
  }

  if (!check_Digilock()){return false;}

  int stepuA = 30;
  int step_xbeam = round(stepuA / (20 * 0.055));

  int start_xbeam = constrain(round(xbeamGoodADC + xbeam_fb_sign*(xbeam_nrecoveryAttempts+1)*xbeamFWHM), 0, 4095);
  xbeamADCout = start_xbeam;
  analogWrite(XBEAM_DAC, xbeamADCout);
  delay(50);

  int i = 0;
  bool success = false;
  uint16_t maxVal = 0;
  uint16_t lastVal = 0;

  while (xbeamADCout < 4095) {
    i++;
    xbeamADCout += step_xbeam;
    analogWrite(XBEAM_DAC, xbeamADCout);
    delay(50);

    if (!check_Digilock()){return false;}
    trackPeaksInRegions();

    uint16_t current = (uint16_t)get_xbeam_iteration_mean();
    if (current > maxVal) {
      maxVal = current;
    }

    if ((float)maxVal > losing_meanThresh * xbeamHeight) {
      Serial.println("Peak found during recovery. Breaking.");
      success = true;
      break;
    }

    lastVal = current;
  }

  if (success) {
    xbeam_fail = false;
    digitalWrite(XBEAM_FAILURE, LOW);
    xbeam_nrecoveryAttempts = 0;
    for (int i = 0; i < BUFFER_SIZE; i++) {
      xbeamRunningBuffer.push((uint16_t)maxVal);
    }
      xbeamGoodADC = xbeamADCout;
      return true;
  } else {
    Serial.println("Recovery failed.");
    return false;
  }
}


void feedbackWrapper() {

  if (!check_Digilock()){return;}
  
  float slower_running_mean = slowerRunningBuffer.getMean();
  float slower_running_std = slowerRunningBuffer.getStd();

  // Serial.print("Slower running mean / std: ");Serial.print(slower_running_mean);Serial.print("/");Serial.println(slower_running_std);

  if (slower_fail) {
    bool slower_recovered = recover_slower();
    if (slower_recovered) {
      digitalWrite(SLOWER_FAILURE, LOW);
      digitalWrite(SLOWER_STATUS, HIGH);
      }
  }
  
  else if (digitalRead(SLOWER_ENABLE)&&((slower_running_mean < losing_meanThresh*slowerHeight)||(slower_running_std > losing_stdThresh*slowerStd))){ // if below threshold or bouncing
    Serial.println("Losing slower detected");
    if (slower_running_mean < lost_meanThresh*slowerHeight){ // if lost
      // digitalWrite(slowerLostPin, HIGH);
      Serial.println("Lost slower detected");
      int bump = constrain(round(slowerADCout + slower_fb_sign * slowerFWHM), 0, 4095); // bump by FWHM in fb_sign direction
      if ((bump == 0)||(bump == 4095)) {Serial.println("Slower at limit!");slower_at_limit = true; analogWrite(SLOWER_DAC, 2048); return;} // check limits
      analogWrite(SLOWER_DAC, bump);
      slowerADCout = bump;
      delay(50);
      trackPeaksInRegions(); // resample
      if ((float)slowerRunningBuffer.latest() > lost_meanThresh*slowerHeight){relock_slower(0);} // run relock algo if bump landed you on the safe tail. BUT WHAT IF YOU BUMP TOO FAR OR NOT FAR ENOUGH?
      else {
        Serial.println("Slower bump 1 failed. Bumping other way.");
        bump = constrain(round(slowerADCout - 2*slower_fb_sign*slowerFWHM), 0, 4095); // otherwise bump in opposite direction and try again
        if ((bump == 0)||(bump == 4095)) {slower_at_limit = true; analogWrite(SLOWER_DAC, 2048); return;}
        analogWrite(SLOWER_DAC, bump);
        slowerADCout = bump;
        delay(50);
        trackPeaksInRegions();
        if ((float)slowerRunningBuffer.latest() > lost_meanThresh*slowerHeight){relock_slower(0);}
        else {Serial.println("Slower bump 2 failed. Exiting."); slower_fail = true;}
      }
    }
    else {
      bump_slower_up();
    }
  }

  if (!check_Digilock()){return;}
  float xbeam_running_mean = xbeamRunningBuffer.getMean();
  float xbeam_running_std = xbeamRunningBuffer.getStd();
  // Serial.print("Xbeam running mean / std: ");Serial.print(xbeam_running_mean);Serial.print("/");Serial.println(xbeam_running_std);

  if (xbeam_fail) {
    bool xbeam_recovered = recover_xbeam();
    if (xbeam_recovered){
      digitalWrite(XBEAM_FAILURE, LOW);
      digitalWrite(XBEAM_STATUS, HIGH);
    }
  }

  else if (digitalRead(XBEAM_ENABLE)&&((xbeam_running_mean < losing_meanThresh*xbeamHeight)||(xbeam_running_std > losing_stdThresh*xbeamStd))){ // if below threshold or bouncing
    Serial.println("Losing xbeam detected");
    if (xbeam_running_mean < lost_meanThresh*xbeamHeight){ // if lost
      Serial.println("Lost xbeam detected");
      // digitalWrite(xbeamLostPin, HIGH);
      int bump = constrain(round(xbeamADCout + xbeam_fb_sign * xbeamFWHM), 0, 4095); // bump by FWHM in fb_sign direction
      if ((bump == 0)||(bump == 4095)) {Serial.println("Xbeam at limit! Exiting");xbeam_at_limit = true; analogWrite(XBEAM_DAC, 2048); return;} // check limits
      analogWrite(XBEAM_DAC, bump);
      xbeamADCout = bump;
      delay(50);
      trackPeaksInRegions(); // resample
      if ((float)xbeamRunningBuffer.latest() > lost_meanThresh*xbeamHeight){relock_xbeam(0);} // run relock algo if bump landed you on the safe tail
      else {
        Serial.println("Xbeam bump 1 failed. Bumping other way.");
        bump = constrain(round(xbeamADCout - 2*xbeam_fb_sign*xbeamFWHM), 0, 4095); // otherwise bump in opposite direction and try again
        if ((bump == 0)||(bump == 4095)) {xbeam_at_limit = true; analogWrite(XBEAM_DAC, 2048); return;}
        analogWrite(XBEAM_DAC, bump);
        xbeamADCout = bump;
        delay(50);
        trackPeaksInRegions();
        if ((float)xbeamRunningBuffer.latest() > lost_meanThresh*xbeamHeight){relock_xbeam(0);}
        else {Serial.println("Xbeam bump 2 failed. Exiting.");xbeam_fail = true;}
      }
    }
    else {
      relock_xbeam(0);
    }
  }

}

void parse_change_command(String args){
  // Expects args of syntax "[VARIABLE],[VALUE]"
  int comma1 = args.indexOf(',');
  String var = args.substring(0,comma1);
  String val = args.substring(comma1+1);
  if (var == "unlock_thresh") {losing_meanThresh = val.toFloat(); Serial.print("losing_meanThresh successfully changed to: ");Serial.println(losing_meanThresh);return;}
  if (var == "lost_thresh") {lost_meanThresh = val.toFloat(); Serial.print("lost_meanThresh successfully changed to: ");Serial.println(lost_meanThresh);return;}
  if (var == "bump_thresh") {bump_thresh = val.toFloat(); Serial.print("bump_thresh successfully changed to: ");Serial.println(bump_thresh);return;}
  if (var == "relock_thresh") {relock_thresh = val.toFloat(); Serial.print("relock_thresh successfully changed to: ");Serial.println(relock_thresh);return;}
  if (var == "std_thresh") {losing_stdThresh = val.toFloat(); Serial.print("losing_stdThresh successfully changed to: ");Serial.println(losing_stdThresh);return;}
  if (var == "slower_sign") {slower_fb_sign = val.toInt(); Serial.print("slower_fb_sign successfully changed to: ");Serial.println(slower_fb_sign);return;}
  if (var == "xbeam_sign") {xbeam_fb_sign = val.toInt(); Serial.print("xbeam_fb_sign successfully changed to: ");Serial.println(xbeam_fb_sign);return;}
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

void loop() {
 String command = SerialUSB.readStringUntil('\n');
 command.trim();

 if (command.startsWith("D")) { // change the trigger holdoff
   delayus = command.substring(1).toInt();
   SerialUSB.print("delayus set to: ");
   SerialUSB.println(delayus);
   initialize_peak_vals_locations();
 }

 if (command == "I") {
   initialize_peak_vals_locations();
 }

 if (command == "FB") {
  if (feedbackActive) {feedbackActive = false; slower_nrecoveryAttempts=0; xbeam_nrecoveryAttempts=0; return;}
  if (!feedbackActive) {feedbackActive = true; return;}
 }

 if (command.startsWith("S")){ // trigger SPC. synatx: "S[startmA],[stopmA],[stepuA]"
  String args = command.substring(1);

  // Find the commas
  int comma1 = args.indexOf(',');
  int comma2 = args.indexOf(',', comma1 + 1);

  if (comma1 > 0 && comma2 > comma1) {
    // Extract substrings between delimiters
    String s1 = args.substring(0, comma1);
    String s2 = args.substring(comma1 + 1, comma2);
    String s3 = args.substring(comma2 + 1);

    // Convert to integers
    float startmA = s1.toFloat();
    float stopmA = s2.toFloat();
    float stepuA = s3.toFloat();
  spectralPurityCurve(startmA,stopmA,stepuA);
  }  

  else {
    spectralPurityCurve(-1.5,1.5,25.0);
  }
 }

 if (command.startsWith("C")){ // change a param. syntax: "C[VARIABLE],[VALUE]"
  parse_change_command(command.substring(1));
 }

 if ((highClusterMeans[0] > 0) && initialized && SPC_init) {
   trackPeaksInRegions();
   if (feedbackActive) {feedbackWrapper();}
 }

 if (command == "ZS"){ // zero the slower output
  slowerADCout = 2048;
  analogWrite(SLOWER_DAC, slowerADCout);
  slower_fail = false;
  slower_nrecoveryAttempts = 0;
  feedbackActive = false;
 }

 if (command == "ZX"){ // zero the xbeams output
  xbeamADCout = 2048;
  analogWrite(XBEAM_DAC, xbeamADCout);
  xbeam_fail = false;
  xbeam_nrecoveryAttempts = 0;
  feedbackActive = false;
 }

 if (command == "Z"){ // zero both outputs
  slowerADCout = 2048;
  analogWrite(SLOWER_DAC, slowerADCout);
  xbeamADCout = 2048;
  analogWrite(XBEAM_DAC, xbeamADCout);
 }

 if (command == "R"){ // read peaks
  sendTrace();
  if (initialized) {printPeakStatus();}
 }
}