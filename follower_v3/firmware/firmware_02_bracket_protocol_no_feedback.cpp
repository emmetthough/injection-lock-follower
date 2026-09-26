/*
 Repeated Peak-Finding Fabry-Perot Scan
 Arduino Due Version
*/
#include <math.h>

#define N 500              // Samples per scan

#define ANALOG_PIN A0
#define TRIGGER_PIN 53
#define SLOWER_DAC DAC0;
#define XBEAM_DAC DAC1;

#define HIGH_THRESH 800
#define LOW_THRESH 400

#define NUM_PEAKS 20
#define MAX_SCANS 100
#define NOISE_THRESH 100

#define CLUSTER_GAP 75
#define MAX_CLUSTERS 3

#define XBEAM_MAX 2948
#define XBEAM_MIN 1148

volatile uint16_t buffer[N];
volatile int adcIndex = 0;
volatile int delayus = 200;

volatile int slowerADCout = 2048;
volatile int xbeamADCout = 2048;

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

// ---------- Global stats ----------
PeakStats highStats;
PeakStats lowStats;

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
 lowStats  = computePeakStats(lowPeaks, lowPeakPos, foundLow);

 int nHighClusters = clusterPeaks(highPeakPos, foundHigh, highClusterMeans);
 int nLowClusters  = clusterPeaks(lowPeakPos,  foundLow,  lowClusterMeans);

 if (nHighClusters > 0 && nLowClusters > 0) {
   int diff = abs((int)highClusterMeans[0] - (int)lowClusterMeans[0]);
   SEARCH_WINDOW = max(5, diff / 2.5);
 }

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
 }

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
 }
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

void spectralPurityCurve(int startmA, int stopmA, int stepuA){
  
  int step_slower = round(stepuA/(20*0.055));
  int step_xbeam = round(stepuA/(100*0.055));

  int start_slower = round(startmA*50/0.055);
  int stop_slower = round(stopmA*50/0.055);

  int start_xbeam = round(startmA*10/0.055);
  int stop_xbeam = round(stopmA*10/0.055);

  int nsteps_slower = min((stopmA-startmA) / step_slower, 512);
  int nsteps_xbeam = min((stopmA-startmA) / step_xbeam, 512);

  int slower_steps[nsteps_slower];
  int xbeam_steps[nsteps_xbeam];

  uint16_t slower_peaks[nsteps_slower];
  uint16_t xbeam_peaks[nsteps_xbeam];

  for (int i = start_slower; i < nsteps_slower; i++){
    slower_out = slowerADCout + i*step_slower;
    analogWrite(SLOWER_DAC, slower_out);
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
    slower_peaks[i] = slower_peaks_mean / n_slower_peaks;
    slower_steps[i] = i*step_slower;

  }

  analogWrite(XBEAM_DAC, xbeamADCout);

  for (int i = start_xbeam; i < nsteps_xbeam; i++){
    xbeam_out = xbeamADCout + i*step_xbeam;
    analogWrite(XBEAM_DAC, xbeam_out);
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
    xbeam_peaks[i] = xbeam_peaks_mean / n_xbeam_peaks;
    xbeam_steps[i] = i*step_xbeam;

  }

  analogWrite(XBEAM_DAC, xbeamADCout);

  SerialUSB.println("[START] Spectral Purity Curve");
  SerialUSB.println("BEGIN Slower");

  SerialUSB.println("END Slower");

  SerialUSB.println("BEGIN Xbeam");
  
  SerialUSB.println("END Xbeam");
  SerialUSB.println("[END] Spectral Purity Curve")
  
}

// ---------- Setup and loop ----------
void setup() {
 SerialUSB.begin(250000);
 Serial.begin(9600);
 pinMode(ANALOG_PIN, INPUT);
 pinMode(TRIGGER_PIN, INPUT);
 analogWriteResolution(12);
 analogWrite(SLOWER_DAC, 2048); // slower DAC0
 analogWrite(XBEAM_DAC, 2048); // xbeam DAC1
 setupADC();
}

void loop() {
 String command = SerialUSB.readStringUntil('\n');
 command.trim();

 if (command.startsWith("D")) {
   delayus = command.substring(1).toInt();
   SerialUSB.print("delayus set to: ");
   SerialUSB.println(delayus);
   initialize_peak_vals_locations();
 }

 if (command == "I") {
   initialize_peak_vals_locations();
   initialized = true;
 }

 // Always run active monitoring
 if ((highClusterMeans[0] > 0) && initialized) {
   trackPeaksInRegions();
 }

 if (command == "R"){
  sendTrace();
  if (initialized) {printPeakStatus();}
 }
}