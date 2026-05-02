#!/bin/bash
# Run this from a temporary C# project to grab the latest DLLs
echo "Run the following in your temporary C# loader project:"
echo "dotnet publish -c Release -r win-x64 --self-contained false -o ./publish_output"
echo ""
echo "Then copy the Kusto.Language.dll into: src/pykusto_language/bin/"
