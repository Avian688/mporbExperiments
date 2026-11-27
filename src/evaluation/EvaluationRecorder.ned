package mporbexperiments.evaluation;

simple EvaluationRecorder
{
    parameters:
        int numConnections;
        int firstConnectionRanks = default(0);
        int passivePort = default(1000);
        double interval @unit(s) = default(100ms);
        string outputFile;
        @class(mporbexperiments::EvaluationRecorder);
}
