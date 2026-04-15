from git import Repo

def GetGitVersion():
    '''report the git commit/branch/tag on which we are '''
    repo = Repo(".", search_parent_directories=True)
    git = repo.git    

    branchOrTag=git.rev_parse('--abbrev-ref', 'HEAD')

    if branchOrTag == 'HEAD':
        # get tag name
        # since several tags are sometime put on the same commit we want to retrieve all of them
        # and use the last one as reference
        # Note:
        # branchOrTag=`git describe --tags --exact-match` does not provided the latest created tag in case several point to the same place
        currentSha=git.rev_parse('--verify','HEAD')

        # list all tags on the current sha using most recent first:
        allTags=git.tag('--points-at',currentSha,'--sort=-creatordate')
        print (allTags)

        allTagsArray=allTags.split(' ') #create an array (assuming tags are separated by space)

        # if we checkouted a commit with no tag associated, the allTagsArray is empty we can use directly the sha value
        if len(allTagsArray) == 0:
            branchOrTag=git.rev-rev_parse('--short','HEAD') # take the short sha
        else:

            branchOrTag=allTagsArray[0] #first from the list
    else:
        #add the head commit id on the current branch
        branchOrTag="{}[{}]".format(branchOrTag,git.rev_parse('--short', 'HEAD'))

    return branchOrTag
if __name__ == "__main__":
    print (GetGitVersion())
