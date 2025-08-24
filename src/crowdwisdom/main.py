import sys
import warnings
from datetime import datetime
from crowdwisdom.crew import Crowdwisdom


def run():
    print("🚀 Starting CrowdwisdomTrading Aggregator Crew...\n")
    crew_instance = Crowdwisdom().crew()
    result = crew_instance.kickoff()

    print("\n✅ Final Output:")
    print(result)

if __name__ == "__main__":
    run()
